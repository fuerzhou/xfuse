# pylint: disable=missing-docstring, invalid-name, too-many-instance-attributes

from datetime import datetime as dt

import gzip

import itertools as it

import os

from imageio import imread, imwrite

import matplotlib.pyplot as plt

import numpy as np

import pandas as pd

import torch as t

from torchvision.utils import make_grid


DEVICE = t.device('cuda' if t.cuda.is_available() else 'cpu')


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


def collect_items(d):
    d_ = {}
    for k, v in d.items():
        try:
            d_[k] = v.item()
        except (ValueError, AttributeError):
            pass
    return d_


def visualize_batch(batch, **kwargs):
    return plt.imshow(
        np.transpose(
            make_grid(
                batch[:64],
                padding=2,
                normalize=True,
            ),
            (1, 2, 0),
        ),
        **kwargs,
    )


def center_crop(input, target_shape):
    return input[tuple([
        slice((a - b) // 2, (a - b) // 2 + b)
        if b is not None else
        slice(None)
        for a, b in zip(input.shape, target_shape)
    ])]


class Histonet(t.nn.Module):
    def __init__(
            self,
            image,
            data,
            hidden_size=512,
            latent_size=96,
            nf=64,
    ):
        super().__init__()

        num_genes = len(data.columns)

        self._shape = [None, None, *image.shape[:2]]

        self.z_mu = t.nn.Parameter(t.zeros(
            [1, latent_size]
            + [np.ceil(x / 16).astype(int) for x in image.shape[:2]]
        ))
        self.z_sd = t.nn.Parameter(t.zeros(
            [1, latent_size]
            + [np.ceil(x / 16).astype(int) for x in image.shape[:2]]
        ))

        self.decoder = t.nn.Sequential(
            t.nn.ConvTranspose2d(latent_size, 16 * nf, 4, 1, 0, bias=True),
            # x16
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.ConvTranspose2d(16 * nf, 8 * nf, 4, 2, 1, bias=True),
            # x8
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.ConvTranspose2d(8 * nf, 4 * nf, 4, 2, 1, bias=True),
            # x4
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.ConvTranspose2d(4 * nf, 2 * nf, 4, 2, 1, bias=True),
            # x2
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.ConvTranspose2d(2 * nf, nf, 4, 2, 1, bias=True),
            # x1
        )

        self.img_mu = t.nn.Sequential(
            t.nn.Conv2d(nf, nf, 3, 1, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.Conv2d(nf, 3, 3, 1, 1, bias=True),
            t.nn.Sigmoid(),
        )
        self.img_sd = t.nn.Sequential(
            t.nn.Conv2d(nf, nf, 3, 1, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.Conv2d(nf, 3, 3, 1, 1, bias=True),
            t.nn.Softplus(),
        )

        self.lrate = t.nn.Sequential(
            t.nn.Conv2d(nf, nf, 3, 1, 1, bias=True),
            t.nn.LeakyReLU(0.2, inplace=True),
            t.nn.Conv2d(nf, num_genes, 3, 1, 1, bias=True),
        )
        self.logit_mu = t.nn.Parameter(
            t.zeros(1, num_genes, 1, 1),
        )
        self.logit_sd = t.nn.Parameter(
            t.zeros(1, num_genes, 1, 1),
        )

    def decode(self, z):
        state = self.decoder(z)

        img_mu = self.img_mu(state)
        img_sd = self.img_sd(state)
        lrate = self.lrate(state)
        logit = t.distributions.Normal(
            self.logit_mu,
            t.nn.functional.softplus(self.logit_sd),
        ).rsample()

        return (
            z,
            img_mu,
            img_sd,
            lrate,
            logit,
        )

    def forward(self):
        z = t.distributions.Normal(
            self.z_mu,
            t.nn.functional.softplus(self.z_sd),
        ).rsample()

        z, *xs = self.decode(z)

        return (
            z,
            *[center_crop(x, self._shape) for x in xs]
        )


def store_state(model, optimizers, iteration, file):
    t.save(
        dict(
            model=model.state_dict(),
            optimizers=[x.state_dict() for x in optimizers],
            iteration=iteration,
        ),
        file,
    )


def restore_state(model, optimizers, file):
    state = t.load(file)
    model.load_state_dict(state['vae'])
    for optimizer, optimizer_state in zip(optimizers, state['optimizers']):
        optimizer.load_state_dict(optimizer_state)
    return state['iteration']


def run(
        image: np.ndarray,
        label: np.ndarray,
        data: pd.DataFrame,
        latent_size: int,
        output_prefix,
        state=None,
        report_interval=50,
        spike_prior=False,
        anneal_dkl=False,
):
    img_prefix = os.path.join(output_prefix, 'images')
    noise_prefix = os.path.join(output_prefix, 'noise')
    chkpt_prefix = os.path.join(output_prefix, 'checkpoints')

    os.makedirs(output_prefix, exist_ok=True)
    os.makedirs(img_prefix, exist_ok=True)
    os.makedirs(noise_prefix, exist_ok=True)
    os.makedirs(chkpt_prefix, exist_ok=True)

    histonet = Histonet(
        image=image,
        data=data,
        latent_size=latent_size,
    ).to(DEVICE)

    optimizer = t.optim.Adam(
        histonet.parameters(),
        lr=1e-3,
        # betas=(0.5, 0.999),
    )
    if state:
        start_epoch = restore_state(
            histonet,
            [optimizer],
            state,
        )
    else:
        start_epoch = 1

    obs = (
        t.tensor(data.values)
        .float()
        .t()
        .unsqueeze(0)
        .to(DEVICE)
    )

    image = (
        t.tensor(
            image.transpose(2, 0, 1),
            requires_grad=False,
        )
        .unsqueeze(0)
        .float()
        .to(DEVICE)
    )
    image = image / 255

    label = t.eye(int(label.max()) + 1)[label.flatten()].to(DEVICE)

    fixed_noise = t.distributions.Normal(
        t.zeros([
            int(x * s)
            for x, s in zip(histonet.z_mu.shape, [1, 1, 1, 1])
        ]),
        t.ones([
            int(x * s)
            for x, s in zip(histonet.z_sd.shape, [1, 1, 1, 1])
        ]),
    ).sample().to(DEVICE)

    for iteration in it.count(start_epoch):
        def _step():
            z, img_mu, img_sd, lrate, logit = histonet()

            # -* generator loss *-
            lpimg = t.distributions.Normal(img_mu, img_sd).log_prob(image)

            rates = (
                (t.exp(lrate).reshape(*lrate.shape[:2], -1) @ label)
                [:, :, 1:]
                + 1e-10
            )
            # ^ NOTE large memory requirements

            # rates = (
            #     t.stack([
            #         t.bincount(label.flatten(), t.exp(lrate[0, i].flatten()))
            #         for i in range(len(data.columns))
            #     ], dim=-1)
            #     [1:]
            # )
            # ^ NOTE gradient for t.bincount not implemented

            d = t.distributions.NegativeBinomial(
                rates,
                logits=logit.reshape(*logit.shape[:2], -1),
            )
            lpobs = d.log_prob(obs)

            dkl = (
                t.sum(
                    t.distributions.Normal(
                        histonet.z_mu,
                        t.nn.functional.softplus(histonet.z_sd),
                    )
                    .log_prob(z)
                    -
                    t.distributions.Normal(0., 1.).log_prob(z)
                )
                +
                t.sum(
                    t.distributions.Normal(
                        histonet.logit_mu,
                        t.nn.functional.softplus(histonet.logit_sd),
                    )
                    .log_prob(logit)
                    -
                    t.distributions.Normal(0., 1.).log_prob(logit)
                )
            )

            loss = - (t.sum(lpobs) + t.sum(lpimg)) + dkl

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            return collect_items({
                'loss':
                loss,
                'p(lab|z)':
                t.mean(t.exp(lpobs)),
                'rmse':
                t.sqrt(t.mean((d.mean - obs) ** 2)),
                'p(img|z)':
                t.mean(t.sigmoid(lpimg)),
                'dqp':
                dkl,
            })

        t.enable_grad()
        histonet.train()

        def _postprocess(iteration, output):
            print(
                f'iteration {iteration:4d}:',
                '  //  '.join([
                    '{} = {:>9s}'.format(k, f'{v:.2e}')
                    for k, v in output.items()
                ]),
            )

            if iteration % 50000 == 0:
                store_state(
                    histonet,
                    [optimizer],
                    iteration,
                    os.path.join(chkpt_prefix, f'iteration-{iteration:05d}.pkl')
                )

            if iteration % report_interval == 0:
                t.no_grad()
                histonet.eval()

                _, imu, isd, *_ = histonet()
                _, imu_fixed, isd_fixed, *_ = histonet.decode(fixed_noise)

                imwrite(
                    os.path.join(img_prefix, f'iteration-{iteration:05d}.jpg'),
                    ((
                        t.distributions.Normal(imu, isd)
                        .sample()
                        [0]
                        .detach()
                        .cpu()
                        .numpy()
                        .transpose(1, 2, 0)
                    ) * 255).astype(np.uint8),
                )
                imwrite(
                    os.path.join(noise_prefix, f'iteration-{iteration:05d}.jpg'),
                    ((
                        t.distributions.Normal(imu_fixed, isd_fixed)
                        .sample()
                        [0]
                        .detach()
                        .cpu()
                        .numpy()
                        .transpose(1, 2, 0)
                    ) * 255).astype(np.uint8),
                )

                t.enable_grad()
                histonet.train()

        ds = (_postprocess(i, _step()) for i in it.count(1))
        [*ds]
        # _ = {
        #     k: np.mean(vs)
        #     for k, vs in zip_dicts(ds).items()
        # }


def main():
    import argparse as ap

    args = ap.ArgumentParser()

    args.add_argument('data-dir', type=str)

    args.add_argument('--latent-size', type=int, default=100)
    args.add_argument('--spike-prior', action='store_true')
    args.add_argument('--anneal-dkl', action='store_true')

    args.add_argument('--zoom', type=float, default=0.1)
    args.add_argument('--genes', type=int, default=50)

    args.add_argument(
        '--output-prefix',
        type=str,
        default=f'./hssl-{dt.now().isoformat()}',
    )
    args.add_argument('--state', type=str)
    args.add_argument('--report-interval', type=int, default=100)

    opts = vars(args.parse_args())

    data_dir = opts.pop('data-dir')

    image = imread(os.path.join(data_dir, 'image.tif'))
    label = imread(os.path.join(data_dir, 'label.tif'))
    data = pd.read_csv(
        os.path.join(data_dir, 'data.gz'),
        sep=' ',
        header=0,
        index_col=0,
    )

    from scipy.ndimage.interpolation import zoom
    zoom_level = opts.pop('zoom')
    label = zoom(label, (zoom_level, zoom_level), order=0)
    image = zoom(image, (zoom_level, zoom_level, 1))

    data = data[
        data.sum(0)
        [[x for x in data.columns if 'ambiguous' not in x]]
        .sort_values()
        [-opts.pop('genes'):]
        .index
    ]

    print(f'using device: {str(DEVICE):s}')

    run(image, label, data, **opts)


if __name__ == '__main__':
    main()


if False:
    from imageio import imread
    from scipy.ndimage.interpolation import zoom
    data = pd.read_csv('~/histonet-test-data/data.gz', sep=' ').set_index('n')
    s = data.sum(0)
    data = data[s[[x for x in s.index if 'ambiguous' not in x]].sort_values()[-50:].index]
    lab = imread('~/histonet-test-data/label.tif')
    img = imread('~/histonet-test-data/image.tif')
    lab = zoom(lab, (0.1, 0.1), order=0)
    img = zoom(img, (0.1, 0.1, 1))