from functools import partial, wraps

import itertools as it

import re

import signal

from typing import List, Optional, Set, Tuple

import h5py

import numpy as np

import pandas as pd

import scipy.sparse as ss

import torch as t

from ..logging import INFO, log
from ..session import get_default_device
from ..utility.file import extension


__all__ = [
    'compose',
    'zip_dicts',
    'set_rng_seed',
    'center_crop',
    'read_data',
    'design_matrix_from',
    'argmax',
    'integrate_loadings',
    'with_interrupt_handler',
    'chunks_of',
    'find_device',
    'sparseonehot',
    'to_device',
    'with_',
]


def compose(f, *gs):
    return (
        (lambda *args, **kwargs: f(compose(*gs)(*args, **kwargs)))
        if gs != ()
        else f
    )


def zip_dicts(ds):
    d0 = next(ds)
    d = {k: [] for k in d0.keys()}
    for d_ in it.chain([d0], ds):
        for k, v in d_.items():
            try:
                d[k].append(v)
            except AttributeError:
                raise ValueError('dict keys are inconsistent')
    return d


def set_rng_seed(seed: int):
    import random
    i32max = np.iinfo(np.int32).max
    random.seed(seed)
    n_seed = random.choice(range(i32max + 1))
    t_seed = random.choice(range(i32max + 1))
    np.random.seed(n_seed)
    t.manual_seed(t_seed)
    t.backends.cudnn.deterministic = True
    t.backends.cudnn.benchmark = False
    log(INFO, ' / '.join([
        'random rng seeded with %d',
        'numpy rng seeded with %d',
        'torch rng seeded with %d',
    ]), seed, n_seed, t_seed)


def center_crop(input, target_shape):
    return input[tuple([
        slice((a - b) // 2, (a - b) // 2 + b)
        if b is not None else
        slice(None)
        for a, b in zip(input.shape, target_shape)
    ])]


def read_data(
        paths: List[str],
        filter_ambiguous: bool = True,
        num_genes: int = None,
        genes: List[str] = None,
) -> pd.DataFrame:
    def _load_file(path: str) -> pd.DataFrame:
        def _csv(sep):
            return (
                pd.read_csv(path, sep=sep, index_col=0)
                .astype(pd.SparseDtype(np.float32, 0))
            )

        def _h5():
            with h5py.File(path, 'r') as data:
                spmatrix = ss.csr_matrix((
                    data['matrix']['data'],
                    data['matrix']['indices'],
                    data['matrix']['indptr'],
                ))
                return pd.DataFrame.sparse.from_spmatrix(
                    spmatrix.astype(np.float32),
                    columns=data['matrix']['columns'],
                    index=pd.Index(data['matrix']['index'], name='n'),
                )

        parse_dict = {
            r'csv(:?\.(gz|bz2))$':  partial(_csv, sep=','),
            r'tsv(:?\.(gz|bz2))$':  partial(_csv, sep='\t'),
            r'hdf(:?5)$':           _h5,
            r'he5$':                _h5,
            r'h5$':                 _h5,
        }

        log(INFO, 'loading data file %s', path)
        for pattern, parser in parse_dict.items():
            if re.match(pattern, extension(path)) is not None:
                return parser()
        raise NotImplementedError(
            f'no parser for file {path}'
            f' (supported file extension patterns:'
            f' {", ".join(parse_dict.keys())})'
        )

    ks, xs = zip(*[(p, _load_file(p)) for p in paths])
    data = (
        pd.concat(
            xs,
            keys=ks,
            join='outer',
            axis=0,
            sort=False,
            copy=False,
        )
        .fillna(0.)
    )

    data = data.iloc[:, (data.sum(0) > 0).values]

    if genes is not None:
        data_ = pd.DataFrame(
            np.zeros((len(data), len(genes))),
            columns=genes,
            index=data.index,
            dtype=float,
        )
        shared_genes = np.intersect1d(genes, data.columns)
        data_[shared_genes] = data[shared_genes]
        data = data_
    elif filter_ambiguous:
        data = data[[
            x for x in data.columns if 'ambiguous' not in x
        ]]

    if num_genes:
        if isinstance(num_genes, int):
            data = data[
                data.sum(0)
                .sort_values()
                [-num_genes:]
                .index
            ]
        if isinstance(num_genes, list):
            data = data[num_genes]

    return data


def design_matrix_from(
        design: pd.DataFrame,
        covariates: Optional[List[Tuple[str, Set[str]]]] = None,
) -> pd.DataFrame:
    if len(design.columns) == 0:
        return pd.DataFrame(np.zeros((0, len(design))))

    design = (
        design
        [[x for x in sorted(design.columns)]]
        .astype(str)
        .astype('category')
    )

    if covariates is not None:
        missing_covariates = [
            x for x, _ in covariates if x not in design.columns]
        if missing_covariates != []:
            raise ValueError(
                'the following covariates are missing from the design: '
                + ', '.join(missing_covariates)
            )

        for covariate, values in covariates:
            design[covariate].cat.set_categories(sorted(values), inplace=True)
        design = design[[x for x, _ in covariates]]
    else:
        for covariate in design.columns:
            design[covariate].cat.set_categories(
                sorted(design[covariate].cat.categories), inplace=True)

    def _encode(covariate):
        log(INFO, 'encoding design covariate "%s" with %d categories: %s',
            covariate.name, len(covariate.cat.categories),
            ', '.join(covariate.cat.categories))
        return pd.DataFrame(
            (
                np.eye(len(covariate.cat.categories), dtype=int)
                [:, covariate.cat.codes]
            ),
            index=covariate.cat.categories,
        )

    ks, vs = zip(*[(k, _encode(v)) for k, v in design.iteritems()])
    return pd.concat(vs, keys=ks)


def argmax(x: t.Tensor):
    return np.unravel_index(t.argmax(x), x.shape)


def integrate_loadings(
        loadings: t.Tensor,
        label: t.Tensor,
        max_label: int = None,
):
    if max_label is None:
        max_label = t.max(label)
    return (
        t.einsum(
            'btxy,bxyi->it',
            loadings.exp(),
            (
                t.eye(max_label + 1)
                .to(label)
                [label.flatten()]
                .reshape(*label.shape, -1)
                .float()
            ),
        )
    )


def with_interrupt_handler(handler):
    def _decorator(func):
        @wraps(func)
        def _wrapper(*args, **kwargs):
            previous_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, handler)
            func(*args, **kwargs)
            signal.signal(signal.SIGINT, previous_handler)
        return _wrapper
    return _decorator


def chunks_of(n, xs):
    class FillMarker:
        pass
    return map(
        lambda xs: [*filter(lambda x: x is not FillMarker, xs)],
        it.zip_longest(*[iter(xs)]*n, fillvalue=FillMarker),
    )


def find_device(x):
    if isinstance(x, t.Tensor):
        return x.device
    if isinstance(x, list):
        for y in x:
            device = find_device(y)
            if device is not None:
                return device
    if isinstance(x, dict):
        for y in x.values():
            device = find_device(y)
            if device is not None:
                return device
    return None


def sparseonehot(labels, classes=None):
    if classes is None:
        classes = labels.max().item() + 1
    idx = t.stack([t.arange(len(labels)).to(labels), labels])
    return t.sparse.LongTensor(
        idx,
        t.ones(idx.shape[1]).to(idx),
        t.Size([len(labels), classes]),
    )


def to_device(x, device=None):
    if device is None:
        device = get_default_device()
    if isinstance(x, t.Tensor):
        return x.to(device)
    if isinstance(x, list):
        return [to_device(y, device) for y in x]
    if isinstance(x, dict):
        return {k: to_device(v, device) for k, v in x.items()}


def with_(ctx):
    def _decorator(f):
        @wraps(f)
        def _wrapped(*args, **kwargs):
            with ctx:
                return f(*args, **kwargs)
        return _wrapped
    return _decorator
