import os
import pickle
import typing

import numpy as np
import pandas as pd

from omegaconf import DictConfig

from src.utils.logging_utils import get_logger
from src.utils.data_utils import split_into_samples
from src.anomaly_scheme import by_mcc_percentiles

logger = get_logger(name=__name__)


def preprocessing(
    cfg: DictConfig, return_preproc: bool = False
) -> typing.Union[pd.DataFrame, tuple[pd.DataFrame, pd.DataFrame]]:
    
    dir_path: str                   = cfg['dir_path']
    ignore_existing_preproc: bool   = cfg['ignore_existing_preproc']
    drop_tr_type: bool              = cfg['preproc']['drop_tr_type']
    time_delta: int                 = cfg['preproc']['time_delta']
    len_min: int                    = cfg['preproc']['len_min']
    len_max: int                    = cfg['preproc']['len_max']

    preproc_dir_path = os.path.join(dir_path, 'preprocessed')
    if not os.path.exists(preproc_dir_path):
        logger.warning('Preprocessing folder does not exist. Creating...')
        os.mkdir(preproc_dir_path)
    if ignore_existing_preproc:
        logger.info('Preprocessing will ignore all previously saved files')
    
    if (
        os.path.exists(os.path.join(preproc_dir_path, 'preproc_dataset.csv')) and \
        not ignore_existing_preproc
    ):
        data_srt = pd.read_csv(
            os.path.join(preproc_dir_path, 'preproc_dataset.csv')
        )
    else:
        df_transaction = pd.read_csv(os.path.join(dir_path, 'transactions.csv'))
        df_transaction.drop(columns='term_id', axis=1, inplace=True)
        df_transaction = df_transaction.join(
            pd.read_csv(
                os.path.join(dir_path, 'gender_train.csv'),
                index_col='customer_id'
            ),
            on='customer_id',
            how='inner'
        ).rename(columns={'gender': 'target'})
        logger.info('Transfering timestamp to the datetime format')

        def convert_to_daytime(series: pd.Series):
            datetime_strange_format: str = series[0]
            day, time = datetime_strange_format.split(' ')
            day = int(day)
            day += 1

            if day <= 365:
                year = '2017'
            else:
                day -= 365
                year = '2018'
            return f'{year}-{day} {time}'

        df_transaction['tr_datetime'] = df_transaction[['tr_datetime']].apply(
            convert_to_daytime,
            axis = 1
        )

        df_transaction['tr_datetime'] = pd.to_datetime(
            df_transaction['tr_datetime'],
            format='%Y-%j %H:%M:%S'
        )
        logger.info('Done')

        if drop_tr_type:
            logger.info('Dropping tr_type')
            df_transaction.drop(columns='tr_type', axis=1, inplace=True)
            logger.info('Done!')

        if (
            os.path.exists(os.path.join(preproc_dir_path, 'mcc2id.dict')) and \
            not ignore_existing_preproc
        ):
            with open(os.path.join(preproc_dir_path, 'mcc2id.dict'), 'rb') as f:
                mcc2id = dict(pickle.load(f))
        else:
            mcc2id = dict(zip(
                df_transaction['mcc_code'].unique(), 
                np.arange(df_transaction['mcc_code'].nunique()) + 1
            ))
            with open(os.path.join(preproc_dir_path, 'mcc2id.dict'), 'wb') as f:
                pickle.dump(mcc2id, f)

        df_transaction['mcc_code'] = df_transaction['mcc_code'].map(mcc2id)

        df_transaction['is_income'] = (df_transaction['amount'] > 0).astype(np.int32)
        df_transaction['amount'] = df_transaction[['amount', 'is_income']].apply(
            lambda t: np.log(t[0]) if t[1] else np.log(-t[0]),
            axis=1
        )

        if (
            os.path.exists(os.path.join(preproc_dir_path, 'user2id.dict')) and \
            not ignore_existing_preproc
        ):
            with open(os.path.join(preproc_dir_path, 'user2id.dict'), 'rb') as f:
                user2id = dict(pickle.load(f))
        else:
            user2id = dict(zip(
                df_transaction['customer_id'].unique(), 
                np.arange(df_transaction['customer_id'].nunique()) + 1
            ))
            with open(os.path.join(preproc_dir_path, 'user2id.dict'), 'wb') as f:
                pickle.dump(user2id, f)
        df_transaction['customer_id'] = df_transaction['customer_id'].map(user2id)

        data_srt = df_transaction.sort_values(
            ['customer_id', 'tr_datetime']
        ).reset_index(drop=True)

        logger.info('Start splitting into samples')
        split_into_samples(
            data_srt,
            time_delta,
            len_min,
            len_max,
            'customer_id',
            'tr_datetime'
        )
        logger.info('Done!')

        data_srt.to_csv(os.path.join(preproc_dir_path, 'preproc_dataset.csv'))

    if (
        os.path.exists(os.path.join(preproc_dir_path, 'agg_dataset.parquet')) and \
        not ignore_existing_preproc
    ):
        data_agg = pd.read_parquet(os.path.join(preproc_dir_path, 'agg_dataset.parquet'))
    
    else:
        data_agg = data_srt.groupby('sample_label').agg({
            'customer_id': lambda x: x.iloc[0],
            'mcc_code': lambda x: x.tolist(),
            'is_income': lambda x: x.tolist(),
            'amount': lambda x: x.tolist(),
            'target': lambda x: x.sum() / x.count()
        })
        data_agg.to_parquet(os.path.join(preproc_dir_path, 'agg_dataset.parquet'))

    if return_preproc:
        return data_agg, data_srt
    else:
        return data_agg
