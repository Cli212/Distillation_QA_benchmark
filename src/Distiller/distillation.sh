#set hyperparameters
#BERT_DIR=output-bert-base/squad_base_cased_lr3e2_teacher
BERT_DIR=bert-base-uncased-squad-v1
OUTPUT_ROOT_DIR=output-bert-base-student
DATA_ROOT_DIR=../../datasets/squad

STUDENT_CONF_DIR=configs/bert_base_uncased_config/L3.json
accu=1
ep=40
lr=10
## if you use mixup, then the actual batch size will be batch_size * 2
batch_size=32
temperature=8
length=512
torch_seed=9580

NAME=L3_squad_base_lr${lr}e${ep}_bert_student
OUTPUT_DIR=${OUTPUT_ROOT_DIR}/${NAME}

gpu_nums=4


mkdir -p $OUTPUT_DIR

python -m torch.distributed.launch --nproc_per_node=${gpu_nums} examples/question_answering/run_distiller.py \
    --model_type bert \
    --model_type_student bert \
    --data_dir $DATA_ROOT_DIR \
    --model_name_or_path $BERT_DIR \
    --output_dir $OUTPUT_DIR \
    --max_seq_length ${length} \
    --bert_config_file_S $STUDENT_CONF_DIR \
    --do_train \
    --do_eval \
    --doc_stride 128 \
    --per_gpu_train_batch_size ${batch_size} \
    --seed ${torch_seed} \
    --num_train_epochs ${ep} \
    --learning_rate ${lr}e-5 \
    --thread 64 \
    --s_opt1 30 \
    --gradient_accumulation_steps ${accu} \
    --temperature ${temperature} \
    --overwrite_output_dir \
    --save_steps 1000 \
    --do_lower_case \
    --output_encoded_layers false \
    --output_attention_layers false \
    --output_att_score true \
    --output_att_sum false  \
    --kd_loss_weight 1.0 \
    --kd_loss_type ce \
    --tag RB \
