import os
import json
from tqdm import tqdm
from utils import Logger
import torch
from multiprocessing import Pool, cpu_count
import numpy as np
from transformers.models.bert.tokenization_bert import whitespace_tokenize
from torch.utils.data import Dataset, DataLoader, RandomSampler, SequentialSampler, TensorDataset, ConcatDataset
from torch.utils.data.distributed import DistributedSampler
# This file defines data preprocessing methods for various datasets
# These methods changes different data structure in datasets to what we can use for our experiments

# logger = logging.getLogger(__name__)
logger = Logger("all.log",level="debug").logger
MULTI_SEP_TOKENS_TOKENIZERS_SET = {"roberta", "camembert", "bart", "mpnet"}



class MyDataset(Dataset):
    def __init__(self, all_input_ids, all_attention_masks, all_token_type_ids, all_start_positions, all_end_positions):
        super(MyDataset, self).__init__()
        self.all_input_ids = all_input_ids
        self.all_attention_masks = all_attention_masks
        self.all_token_type_ids = all_token_type_ids
        self.all_start_positions = all_start_positions
        self.all_end_positions = all_end_positions

    def __getitem__(self, index):
        input_ids = self.all_input_ids[index]
        attention_masks = self.all_attention_masks[index]
        token_type_ids = self.all_token_type_ids[index]
        start_positions = self.all_start_positions[index]
        end_positions = self.all_end_positions[index]
        return {'input_ids': input_ids,
                'attention_mask': attention_masks,
                'token_type_ids': token_type_ids,
                'start_positions': start_positions,
                "end_positions": end_positions}

    def __len__(self):
        return len(self.all_input_ids)



def example_iter(examples):
    i = 0
    while i < len(examples):
        if (i + 32) >= len(examples):
            # yield [j.context_text for j in examples[i:]],i
            yield examples[i:]
        else:
            # yield [j.context_text for j in examples[i:i+32]], i
            yield examples[i:i + 32]
        i += 32

def augment_data(iter_sample, augmenter):
    result = iter_sample.copy()
    for ii, dd in enumerate(augmenter.augment([i.context_text for i in iter_sample])):
        result[ii].context_text = dd
    return result


class DataProvider:
    def __init__(self, dataset, examples, args, tokenizer=None, augmenter=None, s_tokenizer=None, s_dataset=None, collate_fn=None):
        self.examples = examples
        self.dataset = dataset
        self.augmenter = augmenter
        self.args = args
        self.tokenizer = tokenizer
        self.s_tokenizer = s_tokenizer
        self.s_dataset = s_dataset
        self.collate_fn = collate_fn
        self.batch_size = args.train_batch_size * 2 if augmenter else args.train_batch_size
        self.epoch = 0
        self.dataloader = None
        self.s_dataloader = None

    def build(self):

        if self.args.local_rank not in [-1, 0]:
            torch.distributed.barrier()
        if self.augmenter:
            # new_examples = self.examples.copy()
            # pbar = tqdm(total=int(len(self.examples) / 32) + 1, desc="Data augmentation")
            # for iter_sample in example_iter():
            #     text, i = iter_sample
            #     for ii, dd in enumerate(self.augmenter.augment(text)):
            #         new_examples[i + ii].context_text = dd
            #     pbar.update()
            threads = min(self.args.thread, cpu_count())
            from functools import partial
            with Pool(threads) as p:
                # global examples
                # examples = self.examples
                annotate_ = partial(
                    augment_data,
                    augmenter=self.augmenter
                )
                aug_examples = list(
                    tqdm(
                        p.imap(annotate_, example_iter(self.examples), chunksize=32),
                        total=int(len(self.examples) / 32) + 1,
                        desc="Data augmentation",
                        disable=False,
                    )
                )
            new_examples = []
            for i in aug_examples:
                new_examples.extend(i)
            del aug_examples
            features, dataset = convert_examples_to_features(new_examples, self.tokenizer, self.args.max_seq_length,
                                                             self.args.doc_stride,
                                                             self.args.max_query_length,
                                                             is_training=True,
                                                             threads=self.args.thread
                                                             )
            all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
            all_attention_masks = torch.tensor([f.attention_mask for f in features], dtype=torch.long)
            all_token_type_ids = torch.tensor([f.token_type_ids for f in features], dtype=torch.long)
            all_start_positions = torch.tensor([f.start_position for f in features], dtype=torch.long)
            all_end_positions = torch.tensor([f.end_position for f in features], dtype=torch.long)
            dataset = MyDataset(all_input_ids, all_attention_masks, all_token_type_ids, all_start_positions,
                                all_end_positions)
            new_dataset = ConcatDataset([self.dataset, dataset])
            if self.s_tokenizer:
                s_features, s_dataset = convert_examples_to_features(new_examples, self.s_tokenizer, self.args.max_seq_length,
                                                                 self.args.doc_stride,
                                                                 self.args.max_query_length,
                                                                 is_training=True,
                                                                 threads=self.args.thread
                                                                 )
                all_input_ids = torch.tensor([f.input_ids for f in s_features], dtype=torch.long)
                all_attention_masks = torch.tensor([f.attention_mask for f in s_features], dtype=torch.long)
                all_token_type_ids = torch.tensor([f.token_type_ids for f in s_features], dtype=torch.long)
                all_start_positions = torch.tensor([f.start_position for f in s_features], dtype=torch.long)
                all_end_positions = torch.tensor([f.end_position for f in s_features], dtype=torch.long)
                s_dataset = MyDataset(all_input_ids, all_attention_masks, all_token_type_ids, all_start_positions,
                                    all_end_positions)
                s_new_dataset = ConcatDataset([self.s_dataset, s_dataset])
            train_sampler = RandomSampler(new_dataset) if self.args.local_rank == -1 else DistributedSampler(new_dataset)
            if self.args.local_rank != -1:
                train_sampler.set_epoch(self.epoch)
            self.dataloader = DataLoader(new_dataset, sampler=train_sampler, batch_size=self.batch_size,
                                         collate_fn=self.collate_fn, num_workers=self.args.num_workers)
            if self.s_tokenizer:
                self.s_dataloader = DataLoader(s_new_dataset, sampler=train_sampler, batch_size=self.batch_size,
                                             collate_fn=self.collate_fn, num_workers=self.args.num_workers)
        else:
            train_sampler = RandomSampler(self.dataset) if self.args.local_rank == -1 else DistributedSampler(
                self.dataset)
            if self.args.local_rank != -1:
                train_sampler.set_epoch(self.epoch)
            self.dataloader = DataLoader(self.dataset, sampler=train_sampler, batch_size=self.batch_size,
                                         collate_fn=self.collate_fn, num_workers=self.args.num_workers)
            if self.s_tokenizer:
                self.s_dataloader = DataLoader(self.s_dataset, sampler=train_sampler, batch_size=self.batch_size,
                                               collate_fn=self.collate_fn, num_workers=self.args.num_workers)
        if self.args.local_rank == 0:
            torch.distributed.barrier()

    def __len__(self):
        if len(self.examples)%self.batch_size == 0:
            return int(len(self.examples) / self.batch_size)
        return int(len(self.examples) / self.batch_size)+1

    def __iter__(self):
        ## Lack in handling s_dataloader
        if self.epoch % 5 == 0:
            self.build()
        self.epoch += 1
        if self.s_tokenizer:
            for i in range(self.__len__()):
                yield {"teacher": next(iter(self.dataloader)), "student": next(iter(self.s_dataloader))}
        else:
            for i in range(self.__len__()):
                yield next(iter(self.dataloader))
            # return self.dataloader.__iter__()


class Example(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self,
                 qas_id,
                 question_text,
                 paragraph,
                 answer_text=None,
                 start_position=None,
                 is_impossible=None,
                 answers=None):
        """
            Construct a Extractive QA(squad style) example
            Args:
                qas_id: Unique id for the example
                question_text: text of questions
                paragraph: context sentences
                orig_answer_text: the answer text
                start_position: start_position of the answer in the paragraph
                is_impossible: if it is impossible to get answer from the paragraph
        """
        self.qas_id = qas_id
        self.question_text = question_text
        self.context_text = paragraph
        self.answer_text = answer_text
        self.start_position = start_position
        self.is_impossible = is_impossible
        self.answers = answers

        doc_tokens = []
        char_to_word_offset = []
        prev_is_whitespace = True

        for c in self.context_text:
            if _is_whitespace(c):
                prev_is_whitespace = True
            else:
                if prev_is_whitespace:
                    doc_tokens.append(c)
                else:
                    doc_tokens[-1] += c
                prev_is_whitespace = False
            char_to_word_offset.append(len(doc_tokens) - 1)

        self.doc_tokens = doc_tokens
        self.char_to_word_offset = char_to_word_offset

        # Start and end positions only has a value during evaluation.
        if start_position is not None and not is_impossible:
            self.start_position = char_to_word_offset[start_position]
            self.end_position = char_to_word_offset[
                min(start_position + len(answer_text) - 1, len(char_to_word_offset) - 1)
            ]

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        s = ""
        s += f"qas_id: {self.qas_id}"
        s += f", question_text: {self.question_text}"
        s += f", paragraph: {self.context_text}"
        if self.start_position:
            s += f", start_position: {self.start_position}"
        if self.is_impossible:
            s += f", is_impossible: {self.is_impossible}"
        return s

class SquadResult:
    """
    Constructs a SquadResult which can be used to evaluate a model's output on the SQuAD dataset.
    Args:
        unique_id: The unique identifier corresponding to that example.
        start_logits: The logits corresponding to the start of the answer
        end_logits: The logits corresponding to the end of the answer
    """

    def __init__(self, unique_id, start_logits, end_logits, start_top_index=None, end_top_index=None, cls_logits=None):
        self.start_logits = start_logits
        self.end_logits = end_logits
        self.unique_id = unique_id

        if start_top_index:
            self.start_top_index = start_top_index
            self.end_top_index = end_top_index
            self.cls_logits = cls_logits

class InputFeatures(object):
    """A single set of features of data."""

    """
        Single squad example features to be fed to a model. Those features are model-specific and can be crafted from
        :class:`~transformers.data.processors.squad.SquadExample` using the
        :method:`~transformers.data.processors.squad.squad_convert_examples_to_features` method.
        Args:
            input_ids: Indices of input sequence tokens in the vocabulary.
            attention_mask: Mask to avoid performing attention on padding token indices.
            token_type_ids: Segment token indices to indicate first and second portions of the inputs.
            cls_index: the index of the CLS token.
            p_mask: Mask identifying tokens that can be answers vs. tokens that cannot.
                Mask with 1 for tokens that cannot be in the answer and 0 for token that can be in an answer
            example_index: the index of the example
            unique_id: The unique Feature identifier
            paragraph_len: The length of the context
            token_is_max_context: List of booleans identifying which tokens have their maximum context in this feature object.
                If a token does not have their maximum context in this feature object, it means that another feature object
                has more information related to that token and should be prioritized over this feature for that token.
            tokens: list of tokens corresponding to the input ids
            token_to_orig_map: mapping between the tokens and the original text, needed in order to identify the answer.
            start_position: start of the answer token index
            end_position: end of the answer token index
            encoding: optionally store the BatchEncoding with the fast-tokenizer alignement methods.
        """

    def __init__(
            self,
            input_ids,
            attention_mask,
            token_type_ids,
            cls_index,
            p_mask,
            example_index,
            unique_id,
            paragraph_len,
            token_is_max_context,
            tokens,
            token_to_orig_map,
            start_position,
            end_position,
            is_impossible,
            qas_id: str = None,
            encoding = None,
    ):
        self.input_ids = input_ids
        self.attention_mask = attention_mask
        self.token_type_ids = token_type_ids
        self.cls_index = cls_index
        self.p_mask = p_mask

        self.example_index = example_index
        self.unique_id = unique_id
        self.paragraph_len = paragraph_len
        self.token_is_max_context = token_is_max_context
        self.tokens = tokens
        self.token_to_orig_map = token_to_orig_map

        self.start_position = start_position
        self.end_position = end_position
        self.is_impossible = is_impossible
        self.qas_id = qas_id

        self.encoding = encoding


def load_and_cache_examples(args, tokenizer, mode, return_examples=False, s_tokenizer=None):
    s_dataset = None
    s_features = None
    s_cached_features_file = None
    if args.local_rank not in [-1, 0] and mode != "dev":
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    # Load data features from cache or dataset file
    cached_features_file = os.path.join(args.data_dir, "cached_{}_{}_{}_{}".format(mode, args.task_type,
                                                                                   list(filter(None,
                                                                                               tokenizer.name_or_path.split(
                                                                                                   "/"))).pop(),
                                                                                   str(args.max_seq_length)))
    if s_tokenizer:
        s_cached_features_file = os.path.join(args.data_dir, "cached_{}_{}_{}_{}".format(mode, args.task_type,
                                                                                       list(filter(None,
                                                                                                   s_tokenizer.name_or_path.split(
                                                                                                       "/"))).pop(),
                                                                                       str(args.max_seq_length)))
    examples = read_examples_from_file(args.data_dir, mode, args.task_type)
    if os.path.exists(cached_features_file) and not args.overwrite_cache:
        logger.info("Loading features from cached file %s", cached_features_file)
        features = torch.load(cached_features_file)
        dataset = convert_features_to_dataset(features, is_training=(mode == 'train'))
        ## This place need to be more flexible
    else:
        logger.info("Creating features from dataset file at %s", args.data_dir)
        features, dataset = convert_examples_to_features(examples, tokenizer, args.max_seq_length,
                                                         args.doc_stride,
                                                         args.max_query_length,
                                                         is_training=(mode == 'train'),
                                                         threads=args.thread
                                                         )
        if args.local_rank in [-1, 0]:
            logger.info("Saving features into cached file %s", cached_features_file)
            torch.save(features, cached_features_file)
    if s_tokenizer:
        if os.path.exists(s_cached_features_file) and not args.overwrite_cache:
            logger.info("Loading student features from cached file %s", cached_features_file)
            s_features = torch.load(s_cached_features_file)
            s_dataset = convert_features_to_dataset(s_features, is_training=(mode == 'train'))
        else:
            logger.info("Creating student features from dataset file at %s", args.data_dir)
            s_features, s_dataset = convert_examples_to_features(examples, s_tokenizer, args.max_seq_length,
                                                             args.doc_stride,
                                                             args.max_query_length,
                                                             is_training=(mode == 'train'),
                                                             threads=args.thread
                                                             )
            if args.local_rank in [-1, 0]:
                logger.info("Saving student features into cached file %s", s_cached_features_file)
                torch.save(s_features, s_cached_features_file)

    if args.local_rank == 0 and mode != "dev":
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    if mode == "train":
        all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
        all_attention_masks = torch.tensor([f.attention_mask for f in features], dtype=torch.long)
        all_token_type_ids = torch.tensor([f.token_type_ids for f in features], dtype=torch.long)
        all_start_positions = torch.tensor([f.start_position for f in features], dtype=torch.long)
        all_end_positions = torch.tensor([f.end_position for f in features], dtype=torch.long)
        dataset = MyDataset(all_input_ids, all_attention_masks, all_token_type_ids, all_start_positions,
                            all_end_positions)
        if s_tokenizer:
            all_input_ids = torch.tensor([f.input_ids for f in s_features], dtype=torch.long)
            all_attention_masks = torch.tensor([f.attention_mask for f in s_features], dtype=torch.long)
            all_token_type_ids = torch.tensor([f.token_type_ids for f in s_features], dtype=torch.long)
            all_start_positions = torch.tensor([f.start_position for f in s_features], dtype=torch.long)
            all_end_positions = torch.tensor([f.end_position for f in s_features], dtype=torch.long)
            s_dataset = MyDataset(all_input_ids, all_attention_masks, all_token_type_ids, all_start_positions,
                                all_end_positions)
    # Convert to Tensors and build dataset
    if return_examples:
        return dataset, s_dataset, features, s_features, examples
    return dataset, s_dataset


def read_examples_from_file(data_dir, mode, task_type):
    """Read a SQuAD json file into a list of SquadExample."""
    assert mode in ['train', 'dev']
    if task_type == "squad2":
        input_file = os.path.join(data_dir, f"{mode}-v2.0.json")
    elif task_type=="squad":
        input_file = os.path.join(data_dir, f"{mode}-v1.1.json")
    else:
        pass
    with open(input_file, "r", encoding='utf-8') as reader:
        input_data = json.load(reader)["data"]

    examples = []
    for entry in tqdm(input_data):
        for paragraph in entry["paragraphs"]:
            paragraph_text = paragraph["context"]
            char_to_word_offset = []
            for qa in paragraph["qas"]:
                qas_id = qa["id"]
                question_text = qa["question"]
                is_impossible = qa.get("is_impossible", False)
                start_position = None
                answer_text = None
                answers = []

                if not is_impossible:
                    if mode == "train":
                        answer = qa["answers"][0]
                        answer_text = answer["text"]
                        start_position = answer["answer_start"]
                    else:
                        answers = qa["answers"]
                # if len(qa['answers']) == 0:
                #     is_impossible = True
                #     orig_answer_text = ""
                #     start_position = end_position = 0  # use_cls
                # elif len(qa['answers']) == 1:
                #     answer = qa["answers"][0]
                #     orig_answer_text = answer["text"]
                #     if 'answer_start' in answer.keys():
                #         start_position = answer['answer_start']
                #     if orig_answer_text not in paragraph_text:
                #         logger.warning("Could not find answer")
                #         continue
                #     answer_offset = paragraph_text.index(orig_answer_text)
                #     if start_position:
                #         assert start_position == answer_offset
                #     # answer_offset = answer["answer_start"]
                #     answer_length = len(orig_answer_text)
                #     start_position = answer_offset
                # else:
                #     # for datasets not like squad and have various answers to one question
                #     pass

                example = Example(
                    qas_id=qas_id,
                    question_text=question_text,
                    paragraph=paragraph_text,
                    answer_text=answer_text,
                    start_position=start_position,
                    is_impossible=is_impossible,
                    answers=answers)
                examples.append(example)
    return examples



def convert_examples_to_features(
        examples,
        tokenizer,
        max_seq_length,
        doc_stride,
        max_query_length,
        is_training,
        padding_strategy="max_length",
        threads=1,
        tqdm_enabled=True,
        task=None,
):
    """
    Converts a list of examples into a list of features that can be directly given as input to a model. It is
    model-dependant and takes advantage of many of the tokenizer's features to create the model's inputs.
    Args:
        examples: list of :class:`~transformers.data.processors.squad.SquadExample`
        tokenizer: an instance of a child of :class:`~transformers.PreTrainedTokenizer`
        max_seq_length: The maximum sequence length of the inputs.
        doc_stride: The stride used when the context is too large and is split across several features.
        max_query_length: The maximum length of the query.
        is_training: whether to create features for model evaluation or model training.
        padding_strategy: Default to "max_length". Which padding strategy to use
        threads: multiple processing threads.
        tqdm_enabled: whether enable tqdm
    Returns:
        list of :class:`~transformers.data.processors.squad.SquadFeatures`
    Example::
        processor = SquadV2Processor()
        examples = processor.get_dev_examples(data_dir)
        features = squad_convert_examples_to_features(
            examples=examples,
            tokenizer=tokenizer,
            max_seq_length=args.max_seq_length,
            doc_stride=args.doc_stride,
            max_query_length=args.max_query_length,
            is_training=not evaluate,
        )
    """
    # Defining helper methods
    features = []

    threads = min(threads, cpu_count())
    from functools import partial
    with Pool(threads, initializer=squad_convert_example_to_features_init, initargs=(tokenizer,)) as p:
        annotate_ = partial(
            convert_example_to_features,
            max_seq_length=max_seq_length,
            doc_stride=doc_stride,
            max_query_length=max_query_length,
            padding_strategy=padding_strategy,
            is_training=is_training,
        )
        features = list(
            tqdm(
                p.imap(annotate_, examples, chunksize=32),
                total=len(examples),
                desc="convert squad examples to features",
                disable=not tqdm_enabled,
            )
        )

    new_features = []
    unique_id = 1000000000
    example_index = 0
    for example_features in tqdm(
            features, total=len(features), desc="add example index and unique id", disable=not tqdm_enabled
    ):
        if not example_features:
            continue
        for example_feature in example_features:
            example_feature.example_index = example_index
            example_feature.unique_id = unique_id
            new_features.append(example_feature)
            unique_id += 1
        example_index += 1
    features = new_features
    del new_features
    dataset = convert_features_to_dataset(features, is_training)
    return features, dataset


def convert_features_to_dataset(features, is_training):
    # Convert to Tensors and build dataset
    all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
    all_attention_masks = torch.tensor([f.attention_mask for f in features], dtype=torch.long)
    all_token_type_ids = torch.tensor([f.token_type_ids for f in features], dtype=torch.long)
    all_cls_index = torch.tensor([f.cls_index for f in features], dtype=torch.long)
    all_p_mask = torch.tensor([f.p_mask for f in features], dtype=torch.float)
    all_is_impossible = torch.tensor([f.is_impossible for f in features], dtype=torch.float)

    if not is_training:
        all_feature_index = torch.arange(all_input_ids.size(0), dtype=torch.long)
        dataset = TensorDataset(
            all_input_ids, all_attention_masks, all_token_type_ids, all_feature_index, all_cls_index, all_p_mask
        )
    else:
        all_start_positions = torch.tensor([f.start_position for f in features], dtype=torch.long)
        all_end_positions = torch.tensor([f.end_position for f in features], dtype=torch.long)
        dataset = TensorDataset(
            all_input_ids,
            all_attention_masks,
            all_token_type_ids,
            all_start_positions,
            all_end_positions,
            all_cls_index,
            all_p_mask,
            all_is_impossible,
        )
    return dataset

def convert_example_to_features(
        example, max_seq_length, doc_stride, max_query_length, padding_strategy, is_training
):
    """Convert one single example to feature"""
    features = []
    if is_training and not example.is_impossible:
        # Get start and end position
        start_position = example.start_position
        end_position = example.end_position

        # If the answer cannot be found in the text, then skip this example.
        actual_text = " ".join(example.doc_tokens[start_position: (end_position + 1)])
        cleaned_answer_text = " ".join(whitespace_tokenize(example.answer_text))
        if actual_text.find(cleaned_answer_text) == -1:
            logger.warning("Could not find answer: '%s' vs. '%s'", actual_text, cleaned_answer_text)
            return []

    tok_to_orig_index = []
    orig_to_tok_index = []
    all_doc_tokens = []
    for (i, token) in enumerate(example.doc_tokens):
        orig_to_tok_index.append(len(all_doc_tokens))
        if tokenizer.__class__.__name__ in [
            "RobertaTokenizer",
            "LongformerTokenizer",
            "BartTokenizer",
            "RobertaTokenizerFast",
            "LongformerTokenizerFast",
            "BartTokenizerFast",
        ]:
            sub_tokens = tokenizer.tokenize(token, add_prefix_space=True)
        else:
            sub_tokens = tokenizer.tokenize(token)
        for sub_token in sub_tokens:
            tok_to_orig_index.append(i)
            all_doc_tokens.append(sub_token)

    if is_training and not example.is_impossible:
        tok_start_position = orig_to_tok_index[example.start_position]
        if example.end_position < len(example.doc_tokens) - 1:
            tok_end_position = orig_to_tok_index[example.end_position + 1] - 1
        else:
            tok_end_position = len(all_doc_tokens) - 1

        (tok_start_position, tok_end_position) = _improve_answer_span(
            all_doc_tokens, tok_start_position, tok_end_position, tokenizer, example.answer_text
        )

    spans = []

    truncated_query = tokenizer.encode(
        example.question_text, add_special_tokens=False, truncation=True, max_length=max_query_length
    )

    # Tokenizers who insert 2 SEP tokens in-between <context> & <question> need to have special handling
    # in the way they compute mask of added tokens.
    tokenizer_type = type(tokenizer).__name__.replace("Tokenizer", "").lower()
    sequence_added_tokens = (
        tokenizer.model_max_length - tokenizer.max_len_single_sentence + 1
        if tokenizer_type in MULTI_SEP_TOKENS_TOKENIZERS_SET
        else tokenizer.model_max_length - tokenizer.max_len_single_sentence
    )
    sequence_pair_added_tokens = tokenizer.model_max_length - tokenizer.max_len_sentences_pair

    span_doc_tokens = all_doc_tokens
    while len(spans) * doc_stride < len(all_doc_tokens):

        # Define the side we want to truncate / pad and the text/pair sorting
        if tokenizer.padding_side == "right":
            texts = truncated_query
            pairs = span_doc_tokens
            truncation = "only_second"
        else:
            texts = span_doc_tokens
            pairs = truncated_query
            truncation = "only_first"

        encoded_dict = tokenizer.encode_plus(  # TODO(thom) update this logic
            texts,
            pairs,
            truncation=truncation,
            padding=padding_strategy,
            max_length=max_seq_length,
            return_overflowing_tokens=True,
            stride=max_seq_length - doc_stride - len(truncated_query) - sequence_pair_added_tokens,
            return_token_type_ids=True,
        )

        paragraph_len = min(
            len(all_doc_tokens) - len(spans) * doc_stride,
            max_seq_length - len(truncated_query) - sequence_pair_added_tokens,
        )

        if tokenizer.pad_token_id in encoded_dict["input_ids"]:
            if tokenizer.padding_side == "right":
                non_padded_ids = encoded_dict["input_ids"][: encoded_dict["input_ids"].index(tokenizer.pad_token_id)]
            else:
                last_padding_id_position = (
                        len(encoded_dict["input_ids"]) - 1 - encoded_dict["input_ids"][::-1].index(
                    tokenizer.pad_token_id)
                )
                non_padded_ids = encoded_dict["input_ids"][last_padding_id_position + 1:]

        else:
            non_padded_ids = encoded_dict["input_ids"]

        tokens = tokenizer.convert_ids_to_tokens(non_padded_ids)

        token_to_orig_map = {}
        for i in range(paragraph_len):
            index = len(truncated_query) + sequence_added_tokens + i if tokenizer.padding_side == "right" else i
            token_to_orig_map[index] = tok_to_orig_index[len(spans) * doc_stride + i]

        encoded_dict["paragraph_len"] = paragraph_len
        encoded_dict["tokens"] = tokens
        encoded_dict["token_to_orig_map"] = token_to_orig_map
        encoded_dict["truncated_query_with_special_tokens_length"] = len(truncated_query) + sequence_added_tokens
        encoded_dict["token_is_max_context"] = {}
        encoded_dict["start"] = len(spans) * doc_stride
        encoded_dict["length"] = paragraph_len

        spans.append(encoded_dict)

        if "overflowing_tokens" not in encoded_dict or (
                "overflowing_tokens" in encoded_dict and len(encoded_dict["overflowing_tokens"]) == 0
        ):
            break
        span_doc_tokens = encoded_dict["overflowing_tokens"]

    for doc_span_index in range(len(spans)):
        for j in range(spans[doc_span_index]["paragraph_len"]):
            is_max_context = _new_check_is_max_context(spans, doc_span_index, doc_span_index * doc_stride + j)
            index = (
                j
                if tokenizer.padding_side == "left"
                else spans[doc_span_index]["truncated_query_with_special_tokens_length"] + j
            )
            spans[doc_span_index]["token_is_max_context"][index] = is_max_context

    for span in spans:
        # Identify the position of the CLS token
        cls_index = span["input_ids"].index(tokenizer.cls_token_id)

        # p_mask: mask with 1 for token than cannot be in the answer (0 for token which can be in an answer)
        # Original TF implementation also keeps the classification token (set to 0)
        p_mask = np.ones_like(span["token_type_ids"])
        if tokenizer.padding_side == "right":
            p_mask[len(truncated_query) + sequence_added_tokens:] = 0
        else:
            p_mask[-len(span["tokens"]): -(len(truncated_query) + sequence_added_tokens)] = 0

        pad_token_indices = np.where(span["input_ids"] == tokenizer.pad_token_id)
        special_token_indices = np.asarray(
            tokenizer.get_special_tokens_mask(span["input_ids"], already_has_special_tokens=True)
        ).nonzero()

        p_mask[pad_token_indices] = 1
        p_mask[special_token_indices] = 1

        # Set the cls index to 0: the CLS index can be used for impossible answers
        p_mask[cls_index] = 0

        span_is_impossible = example.is_impossible
        start_position = 0
        end_position = 0
        if is_training and not span_is_impossible:
            # For training, if our document chunk does not contain an annotation
            # we throw it out, since there is nothing to predict.
            doc_start = span["start"]
            doc_end = span["start"] + span["length"] - 1
            out_of_span = False

            if not (tok_start_position >= doc_start and tok_end_position <= doc_end):
                out_of_span = True

            if out_of_span:
                start_position = cls_index
                end_position = cls_index
                span_is_impossible = True
            else:
                if tokenizer.padding_side == "left":
                    doc_offset = 0
                else:
                    doc_offset = len(truncated_query) + sequence_added_tokens

                start_position = tok_start_position - doc_start + doc_offset
                end_position = tok_end_position - doc_start + doc_offset

        features.append(
            InputFeatures(
                span["input_ids"],
                span["attention_mask"],
                span["token_type_ids"],
                cls_index,
                p_mask.tolist(),
                example_index=0,
                # Can not set unique_id and example_index here. They will be set after multiple processing.
                unique_id=0,
                paragraph_len=span["paragraph_len"],
                token_is_max_context=span["token_is_max_context"],
                tokens=span["tokens"],
                token_to_orig_map=span["token_to_orig_map"],
                start_position=start_position,
                end_position=end_position,
                is_impossible=span_is_impossible,
                qas_id=example.qas_id,
            )
        )
    return features


def squad_convert_example_to_features_init(tokenizer_for_convert):
    global tokenizer
    tokenizer = tokenizer_for_convert


def _improve_answer_span(doc_tokens, input_start, input_end, tokenizer, orig_answer_text):
    """Returns tokenized answer spans that better match the annotated answer."""
    tok_answer_text = " ".join(tokenizer.tokenize(orig_answer_text))

    for new_start in range(input_start, input_end + 1):
        for new_end in range(input_end, new_start - 1, -1):
            text_span = " ".join(doc_tokens[new_start: (new_end + 1)])
            if text_span == tok_answer_text:
                return (new_start, new_end)

    return (input_start, input_end)


def _check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span.start + doc_span.length - 1
        if position < doc_span.start:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span.start
        num_right_context = end - position
        score = min(num_left_context, num_right_context) + 0.01 * doc_span.length
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index


def _new_check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""
    # if len(doc_spans) == 1:
    # return True
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span["start"] + doc_span["length"] - 1
        if position < doc_span["start"]:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span["start"]
        num_right_context = end - position
        score = min(num_left_context, num_right_context) + 0.01 * doc_span["length"]
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index


def _is_whitespace(c):
    if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F:
        return True
    return False





