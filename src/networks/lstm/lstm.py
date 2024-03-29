import typing
from typing import Any

from omegaconf import DictConfig
from hydra.utils import instantiate

from pytorch_lightning import LightningModule

import numpy as np
import pandas as pd

import torch
from torch import nn

from src.networks.autoencoder_core import CoreBase
from src.networks.common_layers import PositionalEncoding
from src.utils.logging_utils import get_logger
from src.utils.metrtics import f1, r2
from src.utils.data_utils import trim_out_seq, compute_mask, compute_rand_mask

logger = get_logger(name=__name__)


class LSTMAE(LightningModule):

    def __init__(
        self,
        mcc_embed_dim: int,
        n_vocab_size: int,
        loss_weights: tuple[float, float, float],
        freeze_embed: bool,
        unfreeze_after: int,
        lr: float,
        weight_decay: float,
        use_user_embedding: bool,
        user_embedding_size: int,
        use_masked_prediction: bool,
        rand_rate: float,
        mask_token: int,
        core_ae: DictConfig,
        *args: typing.Any, **kwargs: typing.Any
    ) -> None:
        super().__init__(*args, **kwargs)

        self.save_hyperparameters({
            'mcc_embed_dim'         : mcc_embed_dim,
            'n_vocab_size'          : n_vocab_size,
            'loss_weights'          : loss_weights,
            'freeze_embed'          : freeze_embed,
            'unfreeze_after'        : unfreeze_after,
            'lr'                    : lr,
            'weight_decay'          : weight_decay,
            'use_user_embedding'    : use_user_embedding,
            'user_embedding_size'   : user_embedding_size,
            'use_masked_prediction' : use_masked_prediction,
            'rand_rate'             : rand_rate,
            'mask_token'            : mask_token,
            'core_ae'               : core_ae
        })

        n_features = mcc_embed_dim + 2
        if use_user_embedding:
            n_features += user_embedding_size
            self.user_embeds = pd.read_parquet(
                'data/new_data/preprocessed/user_embeddings.parquet'
            )

        self.mcc_embed = nn.Embedding(
            n_vocab_size + 1,
            mcc_embed_dim,
            padding_idx=0
        )
        if freeze_embed:
            with torch.no_grad():
                self.mcc_embed.requires_grad_(False)

        self.pe = PositionalEncoding(mcc_embed_dim)

        self.ae_core: CoreBase = instantiate(core_ae, input_size=n_features)

        self.out_amount = nn.Linear(core_ae['output_size'], 1)
        self.out_binary = nn.Linear(core_ae['output_size'], 1)
        self.out_mcc    = nn.Linear(core_ae['output_size'], n_vocab_size + 1)

        self.amount_loss_weights    = loss_weights[0]
        self.binary_loss_weights    = loss_weights[1]
        self.mcc_loss_weights       = loss_weights[2]

        self.mcc_criterion      = nn.CrossEntropyLoss(ignore_index=0)
        self.binary_criterion   = nn.BCELoss()
        self.amount_criterion   = nn.MSELoss()

        self.training_mcc_f1        = list()
        self.training_binary_f1     = list()
        self.training_amt_r2        = list()
        
        self.val_mcc_f1        = list()
        self.val_binary_f1     = list()
        self.val_amt_r2        = list()

        self.test_mcc_f1        = list()
        self.test_binary_f1     = list()
        self.test_amt_r2        = list()

    # Set pretrained tr2vec weights
    def set_embeds(self, mcc_weights: torch.Tensor):
        with torch.no_grad():
            self.mcc_embed.weight.data = mcc_weights
    
    def on_train_epoch_start(self) -> None:
        if self.hparams['freeze_embed'] \
            and self.current_epoch == self.hparams['unfreeze_after']:
            logger.info('Unfreezing embed weights')
            self.mcc_embed.requires_grad_(True)

        return super().on_train_epoch_start()

    def forward(
        self,
        user_id: torch.Tensor,
        mcc_codes: torch.Tensor,
        is_income: torch.Tensor,
        transaction_amt: torch.Tensor,
        lengths: torch.Tensor,
        mask: torch.Tensor 
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        mcc_embed = self.mcc_embed(mcc_codes)
        mcc_embed = self.pe(mcc_embed, mask)

        is_income = torch.unsqueeze(is_income, -1)
        transaction_amt = torch.unsqueeze(transaction_amt, -1)

        mat_orig = torch.cat(
            (mcc_embed, transaction_amt, is_income),
            -1
        ).float()

        if self.hparams['use_masked_prediction']:
            mat_orig = mat_orig.masked_fill(compute_rand_mask(
                *mat_orig.shape,
                self.hparams['rand_rate'],
                self.device
            ), self.hparams['mask_token'])

        if self.hparams['use_user_embedding']:
            user_embeds = self._compare_user2embed(user_id)

            user_embeds = user_embeds.repeat([mcc_embed.shape[1], 1, 1]).permute((
                1, 0, 2
            )).float()
            user_embeds = user_embeds.masked_fill(~mask, 0)
            mat_orig = torch.cat((user_embeds, mat_orig), -1)

        seqs_after_lstm = self.ae_core(mat_orig, lengths)

        mcc_rec = self.out_mcc(seqs_after_lstm)
        is_income_rec = self.out_binary(seqs_after_lstm)
        amount_rec = self.out_amount(seqs_after_lstm)
        # for is_income we must maintain sigmoid layer by ourselfs
        # as BCEWithLogits will provide for all paddings .5 probability
        is_income_rec = torch.sigmoid(is_income_rec)

        mcc_rec, is_income_rec, amount_rec = trim_out_seq(
            [mcc_rec, is_income_rec, amount_rec], mask
        )

        # squeeze for income and amount is required to reduce last dimension
        return (
            mcc_rec.permute((0, 2, 1)),
            is_income_rec.squeeze(dim=-1),
            amount_rec.squeeze(dim=-1)
        )

    def _compare_user2embed(self, user_ids: torch.Tensor) -> torch.Tensor:
        return torch.tensor(
            self.user_embeds.loc[user_ids.detach().cpu()].values,
        ).to(self.device)
    
    def _calculate_metrics(
        self,
        mcc_probs: torch.Tensor,
        is_income_probs: torch.Tensor,
        amt_value: torch.Tensor,
        mcc_orig: torch.Tensor,
        is_income_orig: torch.Tensor,
        amt_orig: torch.Tensor,
        mask: torch.Tensor
    ) -> tuple[float, float, float, float]:
        with torch.no_grad():
            mcc_probs = torch.argmax(mcc_probs, 1)
            # print(mcc_probs.shape)
            # print(mask.shape)
            mask.squeeze_()
            mcc_probs = mcc_probs.masked_select(mask)
            
            is_income_probs = is_income_probs.masked_select(mask)
            is_income_labels = is_income_probs >= .5

            amt_value = amt_value.masked_select(mask)
            
            mcc_orig = mcc_orig.masked_select(mask)
            is_income_orig = is_income_orig.masked_select(mask)
            amt_orig = amt_orig.masked_select(mask)

            return (
                f1(mcc_probs, mcc_orig, 'macro').item(),
                f1(is_income_labels, is_income_orig).item(),
                r2(amt_value, amt_orig).item()
            )

    
    def _calculate_losses(
        self,
        mcc_rec: torch.Tensor,
        is_income_rec: torch.Tensor,
        amount_rec: torch.Tensor,
        mcc_orig: torch.Tensor,
        is_income_orig: torch.Tensor,
        amount_orig: torch.Tensor
    ) -> tuple[float, tuple[float, float, float]]:
        # Lengths tensor
        
        mcc_loss = self.mcc_criterion(mcc_rec, mcc_orig)
        binary_loss = self.binary_criterion(is_income_rec, is_income_orig)
        amount_loss = self.amount_criterion(amount_rec, amount_orig)

        total_loss = self.mcc_loss_weights * mcc_loss + \
            self.binary_loss_weights * binary_loss + \
            self.amount_loss_weights * amount_loss
        
        return (total_loss, (mcc_loss, binary_loss, amount_loss))
    
    def _all_forward_step(
        self,
        batch: tuple[
            torch.LongTensor,
            torch.LongTensor,
            torch.LongTensor,
            torch.DoubleTensor,
            torch.Tensor,
            torch.Tensor
        ]
    ):
        mask = compute_mask(batch[-2], self.device)
        mcc_rec, is_income_rec, amount_rec = self(*batch[:-1], mask)

        total_loss, (mcc_loss, binary_loss, amount_loss) = self._calculate_losses(
            mcc_rec, is_income_rec, amount_rec, *batch[1:4]
        )

        f1_mcc, f1_binary, r2_amount = self._calculate_metrics(
            mcc_rec, is_income_rec, amount_rec, *batch[1:4], mask
        )

        return(
            total_loss,
            (mcc_loss, binary_loss, amount_loss),
            (f1_mcc, f1_binary, r2_amount)
        )
        
    def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: int = 0) -> Any:
        mask = compute_mask(batch[-2], self.device)
        mcc_rec, is_income_rec, amount_rec = self(*batch[:-1], mask)
        total_loss, (mcc_loss, binary_loss, amount_loss) = self._calculate_losses(
            mcc_rec, is_income_rec, amount_rec, *batch[1:4]
        )

        return(
            total_loss,
            (mcc_loss, binary_loss, amount_loss),
            (mcc_rec, is_income_rec, amount_rec)
        )
    
    def training_step(
        self,
        batch: tuple[
            torch.LongTensor,
            torch.LongTensor,
            torch.LongTensor,
            torch.DoubleTensor,
            torch.Tensor,
            torch.Tensor
        ],
        batch_idx: int
    ) -> float:
        loss, \
        (mcc_loss, binary_loss, amount_loss), \
        (f1_mcc, f1_binary, r2_amount) = self._all_forward_step(
            batch
        )
        self.log('train_loss', loss, prog_bar=True, on_step=True)
        self.log('train_loss_mcc', mcc_loss, on_step=True, prog_bar=False)
        self.log('train_loss_binary', binary_loss, on_step=True, prog_bar=False)
        self.log('train_loss_amt', amount_loss, on_step=True, prog_bar=False)

        self.training_mcc_f1.append(f1_mcc)
        self.training_binary_f1.append(f1_binary)
        self.training_amt_r2.append(r2_amount)

        return loss
    
    def on_train_epoch_end(self) -> None:
        self.log('train_mcc_f1', np.mean(self.training_mcc_f1))
        self.log('train_binary_f1', np.mean(self.training_binary_f1))
        self.log('train_amt_r2', np.mean(self.training_amt_r2))

        self.training_mcc_f1.clear()
        self.training_binary_f1.clear()
        self.training_amt_r2.clear()
    
    def validation_step(
        self,
        batch: tuple[
            torch.LongTensor,
            torch.LongTensor,
            torch.LongTensor,
            torch.DoubleTensor,
            int,
            float
        ],
        batch_idx: int
    ) -> None:
        loss, \
        (mcc_loss, binary_loss, amount_loss), \
        (f1_mcc, f1_binary, r2_amount) = self._all_forward_step(
            batch
        )
        self.log('val_loss', loss, prog_bar=True, on_step=False, on_epoch=True)
        self.log('val_loss_mcc', mcc_loss, on_step=False, on_epoch=True, prog_bar=False)
        self.log('val_loss_binary', binary_loss, on_step=False, on_epoch=True, prog_bar=False)
        self.log('val_loss_amt', amount_loss, on_step=False, on_epoch=True, prog_bar=False)

        self.val_mcc_f1.append(f1_mcc)
        self.val_binary_f1.append(f1_binary)
        self.val_amt_r2.append(r2_amount)

    def on_validation_epoch_end(self) -> None:
        self.log('val_mcc_f1', np.mean(self.val_mcc_f1))
        self.log('val_binary_f1', np.mean(self.val_binary_f1))
        self.log('val_amt_r2', np.mean(self.val_amt_r2))

        self.val_mcc_f1.clear()
        self.val_binary_f1.clear()
        self.val_amt_r2.clear()

    def test_step(
        self,
        batch: tuple[
            torch.LongTensor,
            torch.LongTensor,
            torch.LongTensor,
            torch.DoubleTensor,
            int,
            float
        ],
        batch_idx: int
    ) -> None:
        loss, \
        (mcc_loss, binary_loss, amount_loss), \
        (f1_mcc, f1_binary, r2_amount) = self._all_forward_step(
            batch
        )
        self.log('test_loss', loss, prog_bar=True, on_step=False, on_epoch=True)
        self.log('test_loss_mcc', mcc_loss, on_step=False, on_epoch=True, prog_bar=False)
        self.log('test_loss_binary', binary_loss, on_step=False, on_epoch=True, prog_bar=False)
        self.log('test_loss_amt', amount_loss, on_step=False, on_epoch=True, prog_bar=False)

        self.test_mcc_f1.append(f1_mcc)
        self.test_binary_f1.append(f1_binary)
        self.test_amt_r2.append(r2_amount)

    def on_test_epoch_end(self) -> None:
        self.log('test_mcc_f1', np.mean(self.test_mcc_f1))
        self.log('test_binary_f1', np.mean(self.test_binary_f1))
        self.log('test_amt_r2', np.mean(self.test_amt_r2))

        self.test_mcc_f1.clear()
        self.test_binary_f1.clear()
        self.test_amt_r2.clear()

    def configure_optimizers(self) -> typing.Mapping[str, typing.Any]:
        opt = torch.optim.AdamW(
            self.parameters(),
            self.hparams['lr'],
            weight_decay=self.hparams['weight_decay']
        )
        scheduler =torch.optim.lr_scheduler.ReduceLROnPlateau(
            opt,
            'min',
            1e-1,
            2,
            verbose=True
        )
        return [opt], [{'scheduler': scheduler, 'monitor': 'val_loss'}]
