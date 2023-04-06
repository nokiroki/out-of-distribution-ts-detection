import os

from omegaconf import DictConfig

import torch

from pytorch_lightning import Trainer
from pytorch_lightning.loggers import CometLogger
from pytorch_lightning.callbacks import ModelCheckpoint, EarlyStopping

from preprocessing.new_data_preprop import preprocessing
from networks.tr2vec import Transaction2VecJoint
from datamodules.tr2vec import T2VDatamodule


def train_tr2vec(cfg_preprop: DictConfig, cfg_model: DictConfig, api_token: str) -> None:
    seq_data = preprocessing(cfg_preprop)

    for i in cfg_model['num_iters']:
        model = Transaction2VecJoint(cfg_preprop)

        datamodule = T2VDatamodule(cfg_model, seq_data)

        early_stopping_callback = EarlyStopping(
            monitor='val_loss',
            min_delta=cfg_model['learning_params']['early_stopping_params']['min_delta'],
            min_delta=cfg_model['learning_params']['early_stopping_params']['patience'],
            verbose=True,
            mode='min'
        )

        checkpoint = ModelCheckpoint(
            monitor='val_loss',
            mode='max',
            dirpath=os.path.join('logs', 'checkpoints', 'tr2vec.ckp')
        )

        callbacks = [checkpoint, early_stopping_callback]

        comet_logger = CometLogger(
            api_key=api_token,
            project_name='tr2vec_diploma',
            experiment_name=f'tr2vec_{i}'
        )

        trainer = Trainer(
            accelerator='gpu',
            devices=1,
            log_every_n_steps=20,
            logger=comet_logger,
            deterministic=True,
            callbacks=callbacks,
            max_epochs=cfg_model['learning_params']['max_epochs'],
            auto_lr_find=True
        )

        trainer.fit(model, datamodule=datamodule)

        model_best = Transaction2VecJoint.load_from_checkpoint(checkpoint.best_model_path)

        mcc_emb_size: int = cfg_model['mcc_embed_size']
        window_size: int = cfg_model['window_size']

        torch.save({
                'mccs': model_best.mcc_embedding_layer.weight.data,
                'hidden': model_best.mcc_output.weight.data
            },
            os.path.join(
                'logs',
                'data_weight',
                f'tr2vec_mcc={mcc_emb_size}_window={window_size}.pth',
            )
        )