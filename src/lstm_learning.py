import typing
import os

from omegaconf import DictConfig

import torch

from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from pytorch_lightning.loggers import CometLogger
from pytorch_lightning import Trainer

from sklearn.model_selection import train_test_split

from src.preprocessing.new_data_preprop import preprocessing
from src.datamodules.autoencoder import AEDataModule
from src.networks.lstm import LSTMAE


def train_lstm(
    cfg_preprop: DictConfig, cfg_model: DictConfig, api_token: str
) -> None:
    
    seq_data = preprocessing(cfg_preprop)

    tr2vec_window_size: int = cfg_model['tr2vec']['window_size']
    tr2vec_mcc_embed_size: int = cfg_model['tr2vec']['mcc_embed_size'] 

    weights = torch.load(os.path.join(
        'logs/data_weight',
        f'tr2vec_mcc={tr2vec_mcc_embed_size}_window={tr2vec_window_size}.pth'
    ))

    for i in range(cfg_model['num_iters']):
        seq_data_train, seq_data_test = train_test_split(
            test_size=cfg_model['data_split']['test_size'],
            shuffle=True
        )
        model = LSTMAE(
            cfg_model['embed_dim'],
            cfg_model['num_layers'],
            tr2vec_mcc_embed_size,
            cfg_model['vocab_size'],
            cfg_model['loss_weights'],
            cfg_model['freeze_embed'],
            cfg_model['unfreeze_after'],
            cfg_model['learning_params']['lr'],
            cfg_model['learning_params']['weight_decay']
        )

        datamodule = AEDataModule(
            seq_data_train,
            seq_data_test,
            cfg_preprop['user_column'],
            cfg_preprop['mcc_column'],
            cfg_preprop['transaction_amt_column'],
            cfg_model['data_split']['val_size'],
            cfg_model['learning_params']['batch_size'],
            cfg_preprop['preproc']['len_max'],
            cfg_model['learning_params']['num_workers'],
            .5,
            True,
            True
        )

        # Set pretrained weights
        model.set_embeds(weights)

        early_stop_callback = EarlyStopping(
            monitor='val_loss',
            min_delta=cfg_model['early_stopping_params']['min_delta'],
            patience=cfg_model['early_stopping_params']['patience'],
            verbose=True,
            mode='min'
        )

        checkpoint = ModelCheckpoint(
            os.path.join('logs', 'lstm', 'checkpoints'),
            (
                f"lstm__embed_dim{cfg_model['embed_dim']}_",
                f"num_layers_{cfg_model['num_layers']}"
            ),
            'val_loss',
            mode='min'
        )

        callbacks = [checkpoint, early_stop_callback]

        comet_logger = CometLogger(
            api_token,
            project_name='lstm_ae_diploma',
            experiment_name=f"lstm__embed_dim{cfg_model['embed_dim']}_{i}"
        )

        trainer = Trainer(
            accelerator='gpu',
            devices=1,
            log_every_n_steps=20,
            logger=comet_logger,
            deterministic=True,
            callbacks=callbacks,
            max_epochs=cfg_model['learning_params']['max_epochs']
        )

        # Testing stage
        trainer.fit(model, datamodule=datamodule)
        model_best = LSTMAE.load_from_checkpoint(checkpoint.best_model_path)
        trainer.test(model_best, datamodule=datamodule)
