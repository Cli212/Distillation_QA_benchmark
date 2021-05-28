import os
import json
import glob
import torch
import logging
import random
import ray
from ray import tune
import numpy as np
from ray.tune import CLIReporter
from ray.tune.schedulers import ASHAScheduler

from Distiller.configs import parse
from Distiller.autoaug import AutoAugmenter
from Distiller.utils import Logger, cal_layer_mapping, uploadDirectory, glue_criterion
from Distiller.mp_aug import aug_process
from Distiller.transformers import AutoConfig, AutoTokenizer
from Distiller.transformers import AutoModelForSequenceClassification, AutoModelForQuestionAnswering
from Distiller.textbrewer import DistillationConfig,TrainingConfig,GeneralDistiller, EMDDistiller
import queue
from torch.utils.data import DataLoader, RandomSampler
from torch.utils.data.distributed import DistributedSampler
from Distiller.transformers import AdamW, get_linear_schedule_with_warmup, WEIGHTS_NAME
from torch.multiprocessing import Queue, Process, set_start_method
from ray.tune.integration.torch import DistributedTrainableCreator


logger = logging.getLogger(__name__)
task_dict = {'squad2': AutoModelForQuestionAnswering,
             'squad': AutoModelForQuestionAnswering,
             'glue': AutoModelForSequenceClassification,
             'superglue': AutoModelForSequenceClassification}


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


# class CustomDataLoader(DataLoader):


def train(args, examples, train_dataset, t_model, s_model, tokenizer, augmenter=None, matches=None, predict_callback=None, q=None, processor=None):
    """ Train the model """

    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    # train_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    # mix_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    # train_dataloader = CustomDataLoader(train_dataset, examples, args=args, sampler=train_sampler, batch_size=args.train_batch_size, collate_fn=collate_fn, tokenizer=tokenizer, augmenter=augmenter)
    # train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size, collate_fn=collate_fn)
    if args.aug_pipeline and args.repeated_aug <= 1:
        if args.local_rank not in [-1, 0]:
            torch.distributed.barrier()
        else:
            QUEUE_LIMIT = 600
            count = 0
            while count<QUEUE_LIMIT:
                try:
                    count+=1
                    train_dataset = q.get(timeout=60)
                    torch.save(train_dataset,'train_dataset.bin')
                    break
                except queue.Empty:
                    logger.info("Waiting for data augmentation process to return data")
            if args.local_rank == 0:
                torch.distributed.barrier()
        if args.local_rank not in [-1,0]:
            train_dataset = torch.load('train_dataset.bin')
        # train_dataloader = DataProvider(train_dataset, examples, args, tokenizer, augmenter,s_tokenizer,s_dataset)

    # else:
    #     train_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    #     train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size)
    # mix_dataloader = DataLoader(train_dataset, sampler=mix_sampler,
    #                             batch_size=args.train_batch_size) if args.mixup else None
    train_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    if args.repeated_aug>1:
        def collate_fn(batch):
            return batch
        train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size, collate_fn=collate_fn)
    else:
        train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size,drop_last=True)

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs
        # t_total =
    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {"params": [p for n, p in s_model.named_parameters() if not any(nd in n for nd in no_decay)],
         "weight_decay": args.weight_decay},
        {"params": [p for n, p in s_model.named_parameters() if any(nd in n for nd in no_decay)], "weight_decay": 0.0}
    ]
    scheduler_class = get_linear_schedule_with_warmup
    args.warmup_steps = int(t_total * args.warmup_proportion)
    scheduler_args = {'num_warmup_steps': args.warmup_steps, 'num_training_steps': t_total}
    # if args.fp16:
    #     try:
    #         from apex import amp
    #     except ImportError:
    #         raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
    #     s_model, optimizer = amp.initialize(s_model, optimizer, opt_level=args.fp16_opt_level)

    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1 and args.local_rank == -1:
        t_model = torch.nn.DataParallel(t_model)
        s_model = torch.nn.DataParallel(s_model)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        s_model = torch.nn.parallel.DistributedDataParallel(s_model, device_ids=[args.local_rank],
                                                          output_device=args.local_rank,
                                                          find_unused_parameters=True)
        t_model = torch.nn.parallel.DistributedDataParallel(t_model, device_ids=[args.local_rank],
                                                            output_device=args.local_rank,
                                                            find_unused_parameters=True)
    actual_batch_size = args.per_gpu_train_batch_size
    num_train_steps = len(train_dataloader) // args.gradient_accumulation_steps * actual_batch_size
    if augmenter:
        actual_batch_size *= 2
    if args.mixup:
        actual_batch_size *= 2
    if args.local_rank in [-1,0]:
        # Train!
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_dataset))
        logger.info("  Num Epochs = %d", args.num_train_epochs)
        logger.info("  Instantaneous batch size per GPU = %d", actual_batch_size)
        logger.info("  Total train batch size (w. parallel, distributed & accumulation) = %d",
                    actual_batch_size * args.gradient_accumulation_steps * (
                        torch.distributed.get_world_size() if args.local_rank != -1 else 1))
        logger.info("  Actual train batch size (w. mixup & data augmentation) = %d", actual_batch_size)
        logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
        logger.info("  Total optimization steps = %d", t_total)
    if args.train:
        critic = None
        baseline_fn = None
        if args.intermediate_loss_type == 'mi':
            from Distiller.utils import mlp_critic
            baseline_fn = mlp_critic(t_model.module.config.hidden_size if hasattr(t_model,
                                                          "module") else t_model.config.hidden_size, hidden_size=64, out_dim=1)
            # for name, param in baseline_fn.named_parameters():
            #     if 'weight' in name:
            #         torch.nn.init.xavier_uniform(param)
            #     elif 'bias' in name:
            #         torch.nn.init.constant_(param, 0)
            baseline_fn.to(args.device)
            critic = mlp_critic(t_model.module.config.hidden_size if hasattr(t_model,
                                                          "module") else t_model.config.hidden_size, s_model.module.config.hidden_size if hasattr(s_model,
                                                          "module") else s_model.config.hidden_size, 128, 32)
            critic.to(args.device)
            critic_no_decay=['bias']
            critic_parameters = [
                {"params": [p for n, p in critic.named_parameters() if not any(nd in n for nd in critic_no_decay)],
                 "weight_decay": args.weight_decay},
                {"params": [p for n, p in critic.named_parameters() if any(nd in n for nd in critic_no_decay)],
                 "weight_decay": 0.0},
                {"params": [p for n, p in baseline_fn.named_parameters() if not any(nd in n for nd in critic_no_decay)],
                 "weight_decay": args.weight_decay},
                {"params": [p for n, p in baseline_fn.named_parameters() if any(nd in n for nd in critic_no_decay)],
                 "weight_decay": 0.0}
            ]
            optimizer_grouped_parameters.extend(critic_parameters)
        optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)

        def sigmoid_reverse(x):
            """
                transform alpha logit from range [0.0, 1.0] to [-infty, infty]
            """
            if x == 0.0:
                return -np.inf
            elif x==1.0:
                return np.inf
            else:
                return -np.log(1 / x - 1.)
        args.alpha = sigmoid_reverse(args.alpha)
        if args.intermediate_strategy and args.intermediate_strategy.lower() == "emd":
            distill_config = DistillationConfig(
                temperature=args.temperature,
                hard_label_weight=args.hard_label_weight,
                kd_loss_weight=args.kd_loss_weight,
                kd_loss_type=args.kd_loss_type,
                critic=critic,
                baseline_fn=baseline_fn,
                alpha=args.alpha)
        else:
            # intermediate_matches = matches
            # if args.intermediate_strategy == "skip":
            #     intermediate_matches = []
            #     for match in matches:
            #         intermediate_matches += matches[match]
            logger.info(f"{matches}")
            distill_config = DistillationConfig(
                temperature=args.temperature,
                intermediate_matches=matches,
                hard_label_weight=args.hard_label_weight,
                kd_loss_weight=args.kd_loss_weight,
                kd_loss_type=args.kd_loss_type,
                critic=critic,
                baseline_fn=baseline_fn,
                alpha=args.alpha)
        train_config = TrainingConfig(gradient_accumulation_steps=args.gradient_accumulation_steps, device=args.device,
                                      log_dir=os.path.join(args.output_dir, "log"), output_dir=args.output_dir,
                                      fp16=args.fp16, mixup=args.mixup, local_rank=args.local_rank,
                                      task_type=args.task_type, q=q, augmenter=augmenter, processor=processor, repeated_aug=args.repeated_aug)
        if args.task_type in ["squad", "squad2"]:
            args.task_name = args.task_type
            from Distiller.adapters import BertForQAAdaptor as adaptor_func
        elif args.task_type == "glue":
            from Distiller.adapters import BertForGLUEAdptor as adaptor_func
        adaptor_T = adaptor_func
        adaptor_S = adaptor_func
        if args.intermediate_strategy == "emd":
            distiller = EMDDistiller(train_config=train_config,
                                     distill_config=distill_config,
                                     model_T=t_model, model_S=s_model,
                                     adaptor_T=adaptor_T,
                                     adaptor_S=adaptor_S,
                                     emd=matches)
        else:
            distiller = GeneralDistiller(train_config, distill_config, t_model, s_model, adaptor_T, adaptor_S, )
        with distiller:
            distiller.train(optimizer, scheduler_class=scheduler_class, scheduler_args=scheduler_args, dataloader=train_dataloader,
                            num_epochs=args.num_train_epochs, callback=predict_callback,max_grad_norm=args.max_grad_norm)
            # distiller.train(optimizer,train_dataloader,args.num_train_epochs,
            #                 scheduler_class=scheduler_class, scheduler_args=scheduler_args,
            #                 max_grad_norm=1.0, callback=predict_callback, mixup_value=args.mixup_value,
            #                 mix_dataloader=mix_dataloader, local_rank=args.local_rank)
    return



def remote_fn(config, checkpoint_dir=None, args=None):
    set_start_method('spawn')
    # Set ray tune hyper parameters
    w=[]
    for c in config.items():
        if c[0] == 'intermediate_loss_type' and 'mi' in c[1]:
            args.__setattr__('intermediate_loss_type', c[1].split('_')[0])
            args.__setattr__('alpha', float(c[1].split('_')[1]))
        elif c[0] == "w":
            w = c[1]
        else:
            args.__setattr__(c[0], c[1])
    globals()['best_evaluation'] = 0.0
    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        args.n_gpu = 0 if args.no_cuda else torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend="nccl")
        args.n_gpu = 1
    args.device = device
    # Setup logging
    logging.basicConfig(format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
                        datefmt="%m/%d/%Y %H:%M:%S",
                        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN)
    logger.warning("Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
                   args.local_rank, device, args.n_gpu, bool(args.local_rank != -1), args.fp16)

    # Set seed
    set_seed(args)
    t_config = AutoConfig.from_pretrained(args.T_config_file if args.T_config_file else args.T_model_name_or_path)
    s_config = AutoConfig.from_pretrained(args.S_config_file if args.S_config_file else args.S_model_name_or_path)
    args.model_type = s_config.model_type
    s_config.num_labels = t_config.num_labels
    t_config.output_hidden_states = True
    t_config.output_attentions = True
    s_config.output_hidden_states = True
    s_config.output_attentions = True
    model_class = task_dict.get(args.task_type)
    ## load pretrained models and tokenizers
    t_tokenizer = AutoTokenizer.from_pretrained(args.T_model_name_or_path,
                                                use_fast=False,
                                                config=t_config,
                                                )
    s_tokenizer = AutoTokenizer.from_pretrained(args.S_model_name_or_path,
                                                use_fast=False,
                                                config=s_config) if args.S_model_name_or_path != args.T_model_name_or_path else None
    t_model = model_class.from_pretrained(args.T_model_name_or_path, config=t_config)
    ## If the student borrow layers from teachers, it must borrow complete layers. Their hidden size and attention size
    # must be the same
    if args.random_student:
        s_model = model_class.from_config(s_config)
    else:
        s_model = model_class.from_pretrained(args.S_model_name_or_path, config=s_config)
    s_model.to(args.device)
    t_model.to(args.device)
    # if args.local_rank not in [-1, 0]:
    #     torch.distributed.barrier()
    # else:
    #     print(f"{args.local_rank} Not in barrier")
    #     t_tokenizer = AutoTokenizer.from_pretrained(args.T_model_name_or_path,
    #                                                 use_fast=False,
    #                                                 config=t_config,
    #                                                 )
    #     s_tokenizer = AutoTokenizer.from_pretrained(args.S_model_name_or_path,
    #                                                 use_fast=False,
    #                                                 config=s_config) if args.S_model_name_or_path != args.T_model_name_or_path else None
    #     torch.save(t_tokenizer,'t_tokenizer.bin')
    #     torch.save(s_tokenizer, 's_tokenizer.bin')
    #
    #     t_model = model_class.from_pretrained(args.T_model_name_or_path, config=t_config)
    #     ## If the student borrow layers from teachers, it must borrow complete layers. Their hidden size and attention size
    #     # must be the same
    #     if args.random_student:
    #         s_model = model_class.from_config(s_config)
    #     else:
    #         s_model = model_class.from_pretrained(args.S_model_name_or_path, config=s_config)
    #     torch.save(t_model, 't_model.bin')
    #     torch.save(s_model, 's_model.bin')
    #     if args.local_rank == 0:
    #         torch.distributed.barrier()
    # if args.local_rank not in [-1, 0]:
    #     t_tokenizer = torch.load('t_tokenizer.bin')
    #     s_tokenizer = torch.load('s_tokenizer.bin')
    #     t_model = torch.load('t_model.bin')
    #     s_model = torch.load('s_model.bin')
    if args.local_rank in [-1, 0]:
        logger.info("Training/evaluation parameters %s", args)

    def predict_callback(model, step):
        if args.eval and args.local_rank in [-1, 0]:
            evaluation_result = evaluate_func(args, model, s_tokenizer if s_tokenizer else t_tokenizer, prefix=step)
            global best_evaluation
            if evaluation_result[glue_criterion(args.task_name)[0]] > best_evaluation:
                best_evaluation = evaluation_result[glue_criterion(args.task_name)[0]]
                logger.info("Saving best model checkpoint to %s", os.path.join(args.output_dir, 'best_model'))
                # Save a trained model, configuration and tokenizer using `save_pretrained()`.
                # They can then be reloaded using `from_pretrained()`
                model_to_save = model.module.module if hasattr(model.module,
                                                               "module") else model.module  # Take care of distributed/parallel training
                model_to_save.save_pretrained(os.path.join(args.output_dir, 'best_model'))
                with open(os.path.join(args.output_dir, 'best_model/best_results.txt'), "w") as writer:
                    writer.write(f"Output: {json.dumps(evaluation_result, indent=2)}\n")
            evaluation_result['best_result'] = best_evaluation
            logger.info("***** Eval results *****")
            logger.info(json.dumps(evaluation_result, indent=2) + '\n')
            output_eval_file = os.path.join(args.output_dir, f"{step}_eval_results.txt")
            logger.info(f"Write evaluation result to {output_eval_file}...")
            with open(output_eval_file, "a") as writer:
                writer.write(f"Output: {json.dumps(evaluation_result, indent=2)}\n")
            # if "exact" in evaluation_result.keys():
            #     return evaluation_result['exact'], evaluation_result['f1']
            # else:
            #     return evaluation_result
            model.train()
            return evaluation_result
        else:
            return None

    ## Training
    if args.train:
        # examples = read_examples_from_file(args.data_dir, mode="train", task_type=args.task_type)
        augmenter = None
        q = None
        if args.repeated_aug > 1:
            processor = Processor(args, t_tokenizer, task=args.task_name, max_length=args.max_seq_length,
                                  s_tokenizer=s_tokenizer if s_tokenizer else None)
            train_dataset, s_dataset, features, s_features, examples = processor.load_and_cache_examples(
                mode="train",
                return_examples=True)
        else:
            train_dataset, s_dataset, features, s_features, examples = load_and_cache_examples(args, t_tokenizer,
                                                                                               mode="train",
                                                                                               return_examples=True,
                                                                                               s_tokenizer=s_tokenizer)
        # if args.augmenter_config_path:
        #     augmenter = AutoAugmenter.from_config(args.augmenter_config_path, "cpu" if args.n_gpu == 0 else "gpu")
        #     # global q
        #     q = Queue()
        #     process = Process(target=aug_process,
        #                       args=(q, examples, train_dataset, augmenter, args, t_tokenizer, s_tokenizer))
        #     process.start()
        # if args.aug_type:
        #     augmenter = AutoAugmenter.from_config(args.aug_type)
        #     q = Queue()
        #     process = Process(target=aug_process,
        #                       args=(q, examples, train_dataset, augmenter, args, t_tokenizer, s_tokenizer))
        #     process.start()
        #     # process.join()
        matches = cal_layer_mapping(args, t_config, s_config)
        if args.aug_pipeline and args.repeated_aug <= 1:
            q = Queue()
            if args.local_rank not in [-1, 0]:
                torch.distributed.barrier()
            else:
                augmenter = AutoAugmenter.init_pipeline(w=w)
                if len(augmenter) and args.repeated_aug <= 1:
                    # args.augs = augmenter.aug_names
                    # generate_aug_data(examples, train_dataset, augmenter, args, t_tokenizer, s_tokenizer,32)
                    # q.put(augmenter)
                    process = Process(target=aug_process,
                                      args=(q, examples, train_dataset, augmenter, args, t_tokenizer, s_tokenizer))

                    # train_dataset = generate_aug_data(examples, train_dataset, augmenter, args, t_tokenizer, s_tokenizer)
                    process.start()
                    # process.join()
                if args.local_rank == 0:
                    torch.distributed.barrier()
        elif args.aug_pipeline and args.repeated_aug>1:
            augmenter = AutoAugmenter.init_pipeline(w=w)
        else:
            pass
        train(args, examples, train_dataset, t_model, s_model, t_tokenizer, augmenter, matches, predict_callback,
              q=q, processor=processor if args.repeated_aug > 1 else None)
        # p = Process(target=data_aug_process, args=(augmenter,examples,tokenizer,args))
        # p.start()
        # if args.S_model_name_or_path != args.T_model_name_or_path:
        #     s_train_dataset = load_and_cache_examples(args, s_tokenizer, mode="train", model_name_or_path=args.S_model_name_or_path, examples=examples)
        # train(args, examples, train_dataset, t_model, s_model, t_tokenizer, augmenter, matches, predict_callback,s_tokenizer, s_dataset)

    # Saving best-practices: if you use defaults names for the model, you can reload it using from_pretrained()
    if args.train and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        # Create output directory if needed
        if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
            os.makedirs(args.output_dir)

        logger.info("Saving model checkpoint to %s", args.output_dir)
        # Save a trained model, configuration and tokenizer using `save_pretrained()`.
        # They can then be reloaded using `from_pretrained()`
        model_to_save = s_model.module if hasattr(s_model,
                                                  "module") else s_model  # Take care of distributed/parallel training
        model_to_save.save_pretrained(args.output_dir)
        if s_tokenizer:
            s_tokenizer.save_pretrained(args.output_dir)
        else:
            t_tokenizer.save_pretrained(args.output_dir)
        torch.save(args, os.path.join(args.output_dir, "training_args.bin"))
        with open(os.path.join(args.output_dir, "training_args.json"), 'w') as f:
            arg_dict = vars(args)
            arg_dict['device'] = str(arg_dict['device'])
            json.dump(arg_dict, f)
        uploadDirectory(args.output_dir)
        model = model_class.from_pretrained(args.output_dir)  # , force_download=True)
        model.to(args.device)
        # Good practice: save your training arguments together with the trained model

    # Evaluation
    results = {}
    if args.eval and args.local_rank in [-1, 0]:
        if args.train:
            logger.info("Loading checkpoints saved during training for evaluation")
            checkpoints = [args.output_dir]
            if args.eval_all_checkpoints:
                checkpoints = list(
                    os.path.dirname(c)
                    for c in sorted(glob.glob(args.output_dir + "/**/" + WEIGHTS_NAME, recursive=True))
                )

        else:
            logger.info("Loading checkpoint %s for evaluation", args.S_model_name_or_path)
            checkpoints = [args.S_model_name_or_path]

        if args.eval_all_checkpoints:
            checkpoints = list(os.path.dirname(c) for c in
                               sorted(glob.glob(args.output_dir + "/**/" + WEIGHTS_NAME, recursive=True)))
            logging.getLogger("pytorch_transformers.modeling_utils").setLevel(logging.WARN)  # Reduce logging
        logger.info("Evaluate the following checkpoints: %s", checkpoints)
        for checkpoint in checkpoints:
            global_step = checkpoint.split("-")[-1] if len(checkpoints) > 1 else ""
            model = model_class.from_pretrained(checkpoint)
            model.to(args.device)
            evaluation_result = evaluate_func(args, model, s_tokenizer if s_tokenizer else t_tokenizer,
                                              prefix=global_step, write_prediction=True)
            logger.info("***** Eval results *****")
            logger.info(json.dumps(evaluation_result, indent=2) + '\n')

            output_eval_file = os.path.join(args.output_dir, "final_eval_results.txt")
            logger.info(f"Write evaluation result to {output_eval_file}...")
            with open(output_eval_file, "a") as writer:
                writer.write(f"Output: {json.dumps(evaluation_result, indent=2)}\n")
    return


def main(args, gpus_per_trial=4):
    w_list = [[0], [1], [2], [0, 1], [1, 0], [0, 2], [2, 0], [1, 2], [2, 1], [0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0],
              [2, 0, 1], [2, 1, 0]]
    search_space = {
        "mixup": tune.choice([True,False]),
        "repeated_aug": tune.choice([1,4]),
        "w": tune.choice(w_list)
    }
    # search_space = {
    #     "intermediate_strategy": tune.choice(["skip", "last", "EMD"]),
    #     "kd_loss_type": tune.choice(["ce", "mse"]),
    #     "intermediate_loss_type": tune.choice(["ce", "mse", "cos", "pkd","mi_0.0","mi_0.1","mi_0.5","mi_0.9","mi_1.0"])}
    # search_space = {
    #     "alpha": tune.grid_search([0.0, 0.1, 0.5, 0.9, 1.0]),
    #     "intermediate_strategy": tune.grid_search(["skip", "last", "EMD"]),
    #     "mixup": tune.grid_search([True, False])}
    scheduler = ASHAScheduler(
        metric="accuracy",
        mode="max",
        max_t=args.num_train_epochs,
        grace_period=10,
        reduction_factor=3,
        brackets=1)

    reporter = CLIReporter(
        # parameter_columns=["l1", "l2", "lr", "batch_size"],
        metric_columns=["accuracy"])
    from functools import partial
    # distributed_train_cifar = DistributedTrainableCreator(
    #     partial(train_fn, args=args),
    #     num_gpus_per_worker=10,
    #     num_cpus_per_worker=8
    # )
    # distributed_remote_fn = DistributedTrainableCreator(
    #     partial(remote_fn, args=args),
    #     num_workers=4,
    #     num_cpus_per_worker=8,
    #     num_gpus_per_worker=4,
    #     backend="nccl"
    # )
    # result = tune.run(
    #     distributed_remote_fn,
    #     resources_per_trial=None,
    #     config=search_space,
    #     scheduler=scheduler,
    #     progress_reporter=reporter,
    #     queue_trials=True)
    result = tune.run(
        partial(remote_fn, args=args),
        resources_per_trial={
            "cpu": 32,
            "gpu": gpus_per_trial
        },
        config=search_space,
        progress_reporter=reporter,
        num_samples=30,
        queue_trials=True)
    with open('/home/ray/ray_results.json','w') as f:
        json.dump(result, f)
    best_trial = result.get_best_trial("accuracy", "max", "last")
    print("Best trial config: {}".format(best_trial.config))
    # print("Best trial final validation loss: {}".format(
    #     best_trial.last_result["loss"]))
    print("Best trial final validation accuracy: {}".format(
        best_trial.last_result["accuracy"]))
    print("Best config: ", best_trial.get_best_config(metric="mean_loss", mode="min"))

    # Get a dataframe for analyzing trial results.
    df = best_trial.results_df


if __name__ == '__main__':
    ray.init(address='auto', _redis_password='5241590000000000')
    args = parse()
    # set_start_method('spawn')
    if args.S_model_name_or_path is None:
        args.S_model_name_or_path = args.T_model_name_or_path
    if args.task_type in ["squad", "squad2"]:
        args.task_name = args.task_type
        from Distiller.evaluate import evaluate_squad as evaluate_func
        from Distiller.squad_preprocess import load_and_cache_examples
    elif args.task_type == "glue":
        from Distiller.evaluate import evaluate_glue as evaluate_func
        from Distiller.glue_preprocess import load_and_cache_examples, Processor
    logger = logging.getLogger(__name__)
    main(args)