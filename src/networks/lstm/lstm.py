import typing
import time
from datetime import timedelta

from pytorch_lightning import LightningModule

import torch
from torch import nn

from src.networks.common_layers import PositionalEncoding


class LSTMAE(LightningModule):

    def __init__(
        self,
        n_features: int,
        embed_dim: int,
        num_layers: int,
        mcc_embed_dim: int,
        n_vocab_size: int,
        *args: typing.Any, **kwargs: typing.Any
    ) -> None:
        super().__init__(*args, **kwargs)

        self.save_hyperparameters({
            'n_features'    : n_features,
            'embed_dim'     : embed_dim,
            'num_layers'    : num_layers,
            'mcc_embed_dim' : mcc_embed_dim,
            'n_vocab_size'  : n_vocab_size
        })

        self.mcc_embed = nn.Embedding(n_vocab_size + 1, mcc_embed_dim, padding_idx=0)
        self.pe = PositionalEncoding(mcc_embed_dim)

        self.encoder1 = nn.LSTM(
            input_size=n_features,
            hidden_size=embed_dim * 2,
            num_layers=num_layers,
            batch_first=True
        )
        self.encoder2 = nn.LSTM(
            input_size=embed_dim * 2,
            hidden_size=embed_dim,
            num_layers=num_layers,
            batch_first=True
        )

        self.decoder1 = nn.LSTM(
            input_size=embed_dim,
            hidden_size=embed_dim * 2,
            num_layers=1,
            batch_first=True
        )
        self.decoder2 = nn.LSTM(
            input_size=embed_dim * 2,
            hidden_size=embed_dim * 2,
            num_layers=1,
            batch_first=True
        )

        self.output_layer = nn.Linear(embed_dim * 2, n_features)

        self.train_time: float = None

    # Set pretrained tr2vec weights
    def set_embeds(self, mcc_weights: torch.Tensor):
        with torch.no_grad():
            self.mcc_embed.weight.data = mcc_weights
    
    def on_train_epoch_start(self) -> None:
        self.train_time = time.time()
        return super().on_train_epoch_start()
    
    def on_train_epoch_end(self) -> None:
        train_time = time.time() - self.train_time
        train_time = str(timedelta(seconds=train_time))

        self.log('train_time', train_time, prog_bar=True, on_step=False, on_epoch=True)
        return super().on_train_epoch_end()

    def forward(
        self,
        user_id: torch.Tensor,
        mcc_codes: torch.Tensor,
        is_income: torch.Tensor,
        transaction_amt: torch.Tensor,
        lengths: torch.Tensor
    ) -> None:
        ...