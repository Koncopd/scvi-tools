from __future__ import annotations

import logging
import warnings
from collections.abc import Iterable as IterableClass
from functools import partial
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import torch
from anndata import AnnData

from scvi import REGISTRY_KEYS, settings
from scvi.data import AnnDataManager, fields
from scvi.data._constants import ADATA_MINIFY_TYPE
from scvi.data._utils import _check_nonnegative_integers, _get_adata_minify_type
from scvi.dataloaders import DataSplitter
from scvi.model._utils import (
    _get_batch_code_from_category,
    _get_var_names_from_manager,
    _init_library_size,
    cite_seq_raw_counts_properties,
    get_max_epochs_heuristic,
    use_distributed_sampler,
)
from scvi.model.base._de_core import _de_core
from scvi.module import TOTALVAE
from scvi.train import AdversarialTrainingPlan, TrainRunner
from scvi.utils import track
from scvi.utils._docstrings import de_dsp, devices_dsp, setup_anndata_dsp

from .base import (
    ArchesMixin,
    BaseMinifiedModeModelClass,
    BaseMudataMinifiedModeModelClass,
    RNASeqMixin,
    VAEMixin,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from typing import Literal

    from mudata import MuData

    from scvi._types import AnnOrMuData, Number

logger = logging.getLogger(__name__)


class TOTALVI(
    RNASeqMixin,
    VAEMixin,
    ArchesMixin,
    BaseMinifiedModeModelClass,
    BaseMudataMinifiedModeModelClass,
):
    """total Variational Inference :cite:p:`GayosoSteier21`.

    Parameters
    ----------
    adata
        AnnOrMuData object that has been registered via :meth:`~scvi.model.TOTALVI.setup_anndata`
        or :meth:`~scvi.model.TOTALVI.setup_mudata`.
    n_latent
        Dimensionality of the latent space.
    gene_dispersion
        One of the following:

        * ``'gene'`` - genes_dispersion parameter of NB is constant per gene across cells
        * ``'gene-batch'`` - genes_dispersion can differ between different batches
        * ``'gene-label'`` - genes_dispersion can differ between different labels
    protein_dispersion
        One of the following:

        * ``'protein'`` - protein_dispersion parameter is constant per protein across cells
        * ``'protein-batch'`` - protein_dispersion can differ between different batches NOT TESTED
        * ``'protein-label'`` - protein_dispersion can differ between different labels NOT TESTED
    gene_likelihood
        One of:

        * ``'nb'`` - Negative binomial distribution
        * ``'zinb'`` - Zero-inflated negative binomial distribution
    latent_distribution
        One of:

        * ``'normal'`` - Normal distribution
        * ``'ln'`` - Logistic normal distribution (Normal(0, I) transformed by softmax)
    empirical_protein_background_prior
        Set the initialization of protein background prior empirically. This option fits a GMM for
        each of 100 cells per batch and averages the distributions. Note that even with this option
        set to `True`, this only initializes a parameter that is learned during inference. If
        `False`, randomly initializes. The default (`None`), sets this to `True` if greater than 10
        proteins are used.
    override_missing_proteins
        If `True`, will not treat proteins with all 0 expression in a particular batch as missing.
    **model_kwargs
        Keyword args for :class:`~scvi.module.TOTALVAE`

    Examples
    --------
    >>> mdata = mudata.read_h5mu(path_to_mudata)
    >>> scvi.model.TOTALVI.setup_mudata(
            mdata, modalities={"rna_layer": "rna", "protein_layer": "prot"}
    >>> vae = scvi.model.TOTALVI(mdata)
    >>> vae.train()
    >>> mdata.obsm["X_totalVI"] = vae.get_latent_representation()

    Notes
    -----
    See further usage examples in the following tutorials:

    1. :doc:`/tutorials/notebooks/multimodal/totalVI`
    2. :doc:`/tutorials/notebooks/multimodal/cite_scrna_integration_w_totalVI`
    3. :doc:`/tutorials/notebooks/scrna/scarches_scvi_tools`
    """

    _module_cls = TOTALVAE
    _LATENT_QZM_KEY = "totalvi_latent_qzm"
    _LATENT_QZV_KEY = "totalvi_latent_qzv"
    _data_splitter_cls = DataSplitter
    _training_plan_cls = AdversarialTrainingPlan
    _train_runner_cls = TrainRunner

    def __init__(
        self,
        adata: AnnOrMuData,
        n_latent: int = 20,
        gene_dispersion: Literal["gene", "gene-batch", "gene-label", "gene-cell"] = "gene",
        protein_dispersion: Literal["protein", "protein-batch", "protein-label"] = "protein",
        gene_likelihood: Literal["zinb", "nb"] = "nb",
        latent_distribution: Literal["normal", "ln"] = "normal",
        empirical_protein_background_prior: str | bool | None = None,
        override_missing_proteins: bool = False,
        **model_kwargs,
    ):
        super().__init__(adata)
        self.protein_state_registry = self.adata_manager.get_state_registry(
            REGISTRY_KEYS.PROTEIN_EXP_KEY
        )
        if (
            fields.ProteinObsmField.PROTEIN_BATCH_MASK in self.protein_state_registry
            and not override_missing_proteins
        ):
            batch_mask = self.protein_state_registry.protein_batch_mask
            msg = (
                "Some proteins have all 0 counts in some batches. "
                "These proteins will be treated as missing measurements; however, "
                "this can occur due to experimental design/biology. "
                "Reinitialize the model with `override_missing_proteins=True`,"
                "to override this behavior."
            )
            warnings.warn(msg, UserWarning, stacklevel=settings.warnings_stacklevel)
            self._use_adversarial_classifier = True
        else:
            batch_mask = None
            self._use_adversarial_classifier = False

        emp_prior = (
            empirical_protein_background_prior
            if empirical_protein_background_prior is not None
            else (self.summary_stats.n_proteins > 10)
        )
        if emp_prior and self.minified_data_type != ADATA_MINIFY_TYPE.LATENT_POSTERIOR:
            prior_mean, prior_scale = self._get_totalvi_protein_priors(adata)
        else:
            prior_mean, prior_scale = None, None

        n_cats_per_cov = (
            self.adata_manager.get_state_registry(REGISTRY_KEYS.CAT_COVS_KEY)[
                fields.CategoricalJointObsField.N_CATS_PER_KEY
            ]
            if REGISTRY_KEYS.CAT_COVS_KEY in self.adata_manager.data_registry
            else None
        )

        n_batch = self.summary_stats.n_batch
        if "n_panel" in self.summary_stats:
            n_panel = self.summary_stats.n_panel
            panel_key = "panel"
        else:
            n_panel = self.summary_stats.n_batch
            panel_key = REGISTRY_KEYS.BATCH_KEY

        use_size_factor_key = REGISTRY_KEYS.SIZE_FACTOR_KEY in self.adata_manager.data_registry
        library_log_means, library_log_vars = None, None
        if (
            not use_size_factor_key
            and self.minified_data_type != ADATA_MINIFY_TYPE.LATENT_POSTERIOR
        ):
            library_log_means, library_log_vars = _init_library_size(self.adata_manager, n_batch)

        self.module = self._module_cls(
            n_input_genes=self.summary_stats.n_vars,
            n_input_proteins=self.summary_stats.n_proteins,
            n_batch=n_batch,
            n_latent=n_latent,
            n_continuous_cov=self.summary_stats.get("n_extra_continuous_covs", 0),
            n_cats_per_cov=n_cats_per_cov,
            gene_dispersion=gene_dispersion,
            protein_dispersion=protein_dispersion,
            gene_likelihood=gene_likelihood,
            latent_distribution=latent_distribution,
            protein_batch_mask=batch_mask,
            protein_background_prior_mean=prior_mean,
            protein_background_prior_scale=prior_scale,
            use_size_factor_key=use_size_factor_key,
            library_log_means=library_log_means,
            library_log_vars=library_log_vars,
            n_panel=n_panel,
            panel_key=panel_key,
            **model_kwargs,
        )
        self.module.minified_data_type = self.minified_data_type
        self._model_summary_string = (
            f"TotalVI Model with the following params: \nn_latent: {n_latent}, "
            f"gene_dispersion: {gene_dispersion}, protein_dispersion: {protein_dispersion}, "
            f"gene_likelihood: {gene_likelihood}, latent_distribution: {latent_distribution}"
        )
        self.init_params_ = self._get_init_params(locals())
        if self.registry_["setup_method_name"] == "setup_mudata":
            original_dict = self.registry_["setup_args"]["modalities"]
            self.modalities = {
                "rna_layer": original_dict.get("rna_layer"),
                "protein_layer": original_dict.get("protein_layer"),
            }
        else:
            self.modalities = None

    @devices_dsp.dedent
    def train(
        self,
        max_epochs: int | None = None,
        lr: float = 4e-3,
        accelerator: str = "auto",
        devices: int | list[int] | str = "auto",
        train_size: float | None = None,
        validation_size: float | None = None,
        shuffle_set_split: bool = True,
        batch_size: int = 256,
        early_stopping: bool = True,
        check_val_every_n_epoch: int | None = None,
        reduce_lr_on_plateau: bool = True,
        n_steps_kl_warmup: int | None = None,
        n_epochs_kl_warmup: int | None = None,
        adversarial_classifier: bool | None = None,
        datasplitter_kwargs: dict | None = None,
        plan_kwargs: dict | None = None,
        external_indexing: list[np.array] = None,
        **kwargs,
    ):
        """Trains the model using amortized variational inference.

        Parameters
        ----------
        max_epochs
            Number of passes through the dataset.
        lr
            Learning rate for optimization.
        %(param_accelerator)s
        %(param_devices)s
        train_size
            Size of training set in the range [0.0, 1.0].
        validation_size
            Size of the test set. If `None`, defaults to 1 - `train_size`. If
            `train_size + validation_size < 1`, the remaining cells belong to a test set.
        shuffle_set_split
            Whether to shuffle indices before splitting. If `False`, the val, train, and test set
            are split in the sequential order of the data according to `validation_size` and
            `train_size` percentages.
        batch_size
            Minibatch size to use during training.
        early_stopping
            Whether to perform early stopping with respect to the validation set.
        check_val_every_n_epoch
            Check val every n train epochs. By default, val is not checked, unless `early_stopping`
            is `True` or `reduce_lr_on_plateau` is `True`. If either of the latter conditions are
            met, val is checked every epoch.
        reduce_lr_on_plateau
            Reduce learning rate on plateau of validation metric (default is ELBO).
        n_steps_kl_warmup
            Number of training steps (minibatches) to scale weight on KL divergences from 0 to 1.
            Only activated when `n_epochs_kl_warmup` is set to None. If `None`, defaults
            to `floor(0.75 * adata.n_obs)`.
        n_epochs_kl_warmup
            Number of epochs to scale weight on KL divergences from 0 to 1.
            Overrides `n_steps_kl_warmup` when both are not `None`.
        adversarial_classifier
            Whether to use adversarial classifier in the latent space. This helps mixing when
            there are missing proteins in any of the batches. Defaults to `True` is missing
            proteins are detected.
        datasplitter_kwargs
            Additional keyword arguments passed into :class:`~scvi.dataloaders.DataSplitter`.
        plan_kwargs
            Keyword args for :class:`~scvi.train.AdversarialTrainingPlan`. Keyword arguments passed
            to `train()` will overwrite values present in `plan_kwargs`, when appropriate.
        external_indexing
            A list of data split indices in the order of training, validation, and test sets.
            Validation and test set are not required and can be left empty.
        **kwargs
            Other keyword args for :class:`~scvi.train.Trainer`.
        """
        if adversarial_classifier is None:
            adversarial_classifier = self._use_adversarial_classifier
        n_steps_kl_warmup = (
            n_steps_kl_warmup if n_steps_kl_warmup is not None else int(0.75 * self.adata.n_obs)
        )
        if reduce_lr_on_plateau:
            check_val_every_n_epoch = 1

        update_dict = {
            "lr": lr,
            "adversarial_classifier": adversarial_classifier,
            "reduce_lr_on_plateau": reduce_lr_on_plateau,
            "n_epochs_kl_warmup": n_epochs_kl_warmup,
            "n_steps_kl_warmup": n_steps_kl_warmup,
        }
        if plan_kwargs is not None:
            plan_kwargs.update(update_dict)
        else:
            plan_kwargs = update_dict

        if max_epochs is None:
            max_epochs = get_max_epochs_heuristic(self.adata.n_obs)

        plan_kwargs = plan_kwargs if isinstance(plan_kwargs, dict) else {}
        datasplitter_kwargs = datasplitter_kwargs or {}

        data_splitter = self._data_splitter_cls(
            self.adata_manager,
            train_size=train_size,
            validation_size=validation_size,
            shuffle_set_split=shuffle_set_split,
            batch_size=batch_size,
            distributed_sampler=use_distributed_sampler(kwargs.get("strategy", None)),
            external_indexing=external_indexing,
            **datasplitter_kwargs,
        )
        training_plan = self._training_plan_cls(self.module, **plan_kwargs)
        runner = self._train_runner_cls(
            self,
            training_plan=training_plan,
            data_splitter=data_splitter,
            max_epochs=max_epochs,
            accelerator=accelerator,
            devices=devices,
            early_stopping=early_stopping,
            check_val_every_n_epoch=check_val_every_n_epoch,
            **kwargs,
        )
        return runner()

    @torch.inference_mode()
    def get_latent_library_size(
        self,
        adata: AnnData | None = None,
        indices: Sequence[int] | None = None,
        give_mean: bool = True,
        batch_size: int | None = None,
    ) -> np.ndarray:
        r"""Returns the latent library size for each cell.

        This is denoted as :math:`\ell_n` in the totalVI paper.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        give_mean
            Return the mean or a sample from the posterior distribution.
        batch_size
            Minibatch size for data loading into model. Defaults to `scvi.settings.batch_size`.
        """
        self._check_if_trained(warn=False)

        adata = self._validate_anndata(adata)
        post = self._make_data_loader(adata=adata, indices=indices, batch_size=batch_size)

        libraries = []
        for tensors in post:
            inference_inputs = self.module._get_inference_input(tensors)
            outputs = self.module.inference(**inference_inputs)
            if give_mean:
                ql = outputs["ql"]
                library = torch.exp(ql.loc + 0.5 * (ql.scale**2))
            else:
                library = outputs["library_gene"]
            libraries += [library.cpu()]
        return torch.cat(libraries).numpy()

    @torch.inference_mode()
    def get_normalized_expression(
        self,
        adata=None,
        indices=None,
        n_samples_overall: int | None = None,
        transform_batch: Sequence[Number | str] | None = None,
        gene_list: Sequence[str] | None = None,
        protein_list: Sequence[str] | None = None,
        library_size: float | Literal["latent"] | None = 1,
        n_samples: int = 1,
        sample_protein_mixing: bool = False,
        scale_protein: bool = False,
        include_protein_background: bool = False,
        batch_size: int | None = None,
        return_mean: bool = True,
        return_numpy: bool | None = None,
        silent: bool = True,
    ) -> tuple[np.ndarray | pd.DataFrame, np.ndarray | pd.DataFrame]:
        r"""Returns the normalized gene expression and protein expression.

        This is denoted as :math:`\rho_n` in the totalVI paper for genes, and TODO
        for proteins, :math:`(1-\pi_{nt})\alpha_{nt}\beta_{nt}`.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        n_samples_overall
            Number of samples to use in total
        transform_batch
            Batch to condition on.
            If transform_batch is:

            - None, then real observed batch is used
            - int, then batch transform_batch is used
            - List[int], then average over batches in list
        gene_list
            Return frequencies of expression for a subset of genes.
            This can save memory when working with large datasets and few genes are
            of interest.
        protein_list
            Return protein expression for a subset of genes.
            This can save memory when working with large datasets and few genes are
            of interest.
        library_size
            Scale the expression frequencies to a common library size.
            This allows gene expression levels to be interpreted on a common scale of relevant
            magnitude.
        n_samples
            Get sample scale from multiple samples.
        sample_protein_mixing
            Sample mixing bernoulli, setting background to zero
        scale_protein
            Make protein expression sum to 1
        include_protein_background
            Include background component for protein expression
        batch_size
            Minibatch size for data loading into model. Defaults to `scvi.settings.batch_size`.
        return_mean
            Whether to return the mean of the samples.
        return_numpy
            Return a `np.ndarray` instead of a `pd.DataFrame`. Includes gene
            names as columns. If either n_samples=1 or return_mean=True, defaults to False.
            Otherwise, it defaults to True.
        %(de_silent)s

        Returns
        -------
        - **gene_normalized_expression** - normalized expression for RNA
        - **protein_normalized_expression** - normalized expression for proteins

        If ``n_samples`` > 1 and ``return_mean`` is False, then the shape is
        ``(samples, cells, genes)``. Otherwise, shape is ``(cells, genes)``. Return type is
        ``pd.DataFrame`` unless ``return_numpy`` is True.
        """
        adata = self._validate_anndata(adata)
        adata_manager = self.get_anndata_manager(adata)
        if indices is None:
            indices = np.arange(adata.n_obs)
        if n_samples_overall is not None:
            indices = np.random.choice(indices, n_samples_overall)
        post = self._make_data_loader(adata=adata, indices=indices, batch_size=batch_size)

        if gene_list is None:
            gene_mask = slice(None)
        else:
            all_genes = _get_var_names_from_manager(adata_manager)
            gene_mask = [True if gene in gene_list else False for gene in all_genes]
        if protein_list is None:
            protein_mask = slice(None)
        else:
            all_proteins = self.protein_state_registry.column_names
            protein_mask = [True if p in protein_list else False for p in all_proteins]
        if indices is None:
            indices = np.arange(adata.n_obs)

        if n_samples > 1 and return_mean is False:
            if return_numpy is False:
                warnings.warn(
                    "`return_numpy` must be `True` if `n_samples > 1` and `return_mean` "
                    "is `False`, returning an `np.ndarray`.",
                    UserWarning,
                    stacklevel=settings.warnings_stacklevel,
                )
            return_numpy = True

        if not isinstance(transform_batch, IterableClass):
            transform_batch = [transform_batch]

        transform_batch = _get_batch_code_from_category(adata_manager, transform_batch)

        scale_list_gene = []
        scale_list_pro = []

        for tensors in post:
            x = tensors[REGISTRY_KEYS.X_KEY]
            y = tensors[REGISTRY_KEYS.PROTEIN_EXP_KEY]
            px_scale = torch.zeros_like(x)[..., gene_mask]
            py_scale = torch.zeros_like(y)[..., protein_mask]
            if n_samples > 1:
                px_scale = torch.stack(n_samples * [px_scale])
                py_scale = torch.stack(n_samples * [py_scale])
            for b in track(transform_batch, disable=silent):
                generative_kwargs = {"transform_batch": b}
                inference_kwargs = {"n_samples": n_samples}
                _, generative_outputs = self.module.forward(
                    tensors=tensors,
                    inference_kwargs=inference_kwargs,
                    generative_kwargs=generative_kwargs,
                    compute_loss=False,
                )
                if library_size == "latent":
                    px_scale += generative_outputs["px_"]["rate"].cpu()[..., gene_mask]
                else:
                    px_scale += generative_outputs["px_"]["scale"].cpu()[..., gene_mask]

                py_ = generative_outputs["py_"]
                # probability of background
                protein_mixing = 1 / (1 + torch.exp(-py_["mixing"].cpu()))
                if sample_protein_mixing is True:
                    protein_mixing = torch.distributions.Bernoulli(protein_mixing).sample()
                protein_val = py_["rate_fore"].cpu() * (1 - protein_mixing)
                if include_protein_background is True:
                    protein_val += py_["rate_back"].cpu() * protein_mixing

                if scale_protein is True:
                    protein_val = torch.nn.functional.normalize(protein_val, p=1, dim=-1)
                protein_val = protein_val[..., protein_mask]
                py_scale += protein_val
            px_scale /= len(transform_batch)
            py_scale /= len(transform_batch)
            scale_list_gene.append(px_scale)
            scale_list_pro.append(py_scale)

        if n_samples > 1:
            # concatenate along batch dimension -> result shape = (samples, cells, features)
            scale_list_gene = torch.cat(scale_list_gene, dim=1)
            scale_list_pro = torch.cat(scale_list_pro, dim=1)
            # (cells, features, samples)
            scale_list_gene = scale_list_gene.permute(1, 2, 0)
            scale_list_pro = scale_list_pro.permute(1, 2, 0)
        else:
            scale_list_gene = torch.cat(scale_list_gene, dim=0)
            scale_list_pro = torch.cat(scale_list_pro, dim=0)

        if return_mean is True and n_samples > 1:
            scale_list_gene = torch.mean(scale_list_gene, dim=-1)
            scale_list_pro = torch.mean(scale_list_pro, dim=-1)

        scale_list_gene = scale_list_gene.cpu().numpy()
        scale_list_pro = scale_list_pro.cpu().numpy()
        if return_numpy is None or return_numpy is False:
            gene_df = pd.DataFrame(
                scale_list_gene,
                columns=_get_var_names_from_manager(adata_manager)[gene_mask],
                index=adata.obs_names[indices],
            )
            protein_names = self.protein_state_registry.column_names
            pro_df = pd.DataFrame(
                scale_list_pro,
                columns=protein_names[protein_mask],
                index=adata.obs_names[indices],
            )

            return gene_df, pro_df
        else:
            return scale_list_gene, scale_list_pro

    @torch.inference_mode()
    def get_protein_foreground_probability(
        self,
        adata: AnnData | None = None,
        indices: Sequence[int] | None = None,
        transform_batch: Sequence[Number | str] | None = None,
        protein_list: Sequence[str] | None = None,
        n_samples: int = 1,
        batch_size: int | None = None,
        return_mean: bool = True,
        return_numpy: bool | None = None,
        silent: bool = True,
    ):
        r"""Returns the foreground probability for proteins.

        This is denoted as :math:`(1 - \pi_{nt})` in the totalVI paper.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        transform_batch
            Batch to condition on.
            If transform_batch is:

            - None, then real observed batch is used
            - int, then batch transform_batch is used
            - List[int], then average over batches in list
        protein_list
            Return protein expression for a subset of genes.
            This can save memory when working with large datasets and few genes are
            of interest.
        n_samples
            Number of posterior samples to use for estimation.
        batch_size
            Minibatch size for data loading into model. Defaults to `scvi.settings.batch_size`.
        return_mean
            Whether to return the mean of the samples.
        return_numpy
            Return a :class:`~numpy.ndarray` instead of a :class:`~pandas.DataFrame`. DataFrame
            includes gene names as columns. If either `n_samples=1` or `return_mean=True`, defaults
            to `False`. Otherwise, it defaults to `True`.
        %(de_silent)s

        Returns
        -------
        - **foreground_probability** - probability foreground for each protein

        If `n_samples` > 1 and `return_mean` is False, then the shape is `(samples, cells, genes)`.
        Otherwise, shape is `(cells, genes)`. In this case, return type is
        :class:`~pandas.DataFrame` unless `return_numpy` is True.
        """
        adata = self._validate_anndata(adata)
        post = self._make_data_loader(adata=adata, indices=indices, batch_size=batch_size)

        if protein_list is None:
            protein_mask = slice(None)
        else:
            all_proteins = self.protein_state_registry.column_names
            protein_mask = [True if p in protein_list else False for p in all_proteins]

        if n_samples > 1 and return_mean is False:
            if return_numpy is False:
                warnings.warn(
                    "`return_numpy` must be `True` if `n_samples > 1` and `return_mean` "
                    "is `False`, returning an `np.ndarray`.",
                    UserWarning,
                    stacklevel=settings.warnings_stacklevel,
                )
            return_numpy = True
        if indices is None:
            indices = np.arange(adata.n_obs)

        py_mixings = []
        if not isinstance(transform_batch, IterableClass):
            transform_batch = [transform_batch]

        transform_batch = _get_batch_code_from_category(self.adata_manager, transform_batch)
        for tensors in post:
            y = tensors[REGISTRY_KEYS.PROTEIN_EXP_KEY]
            py_mixing = torch.zeros_like(y[..., protein_mask])
            if n_samples > 1:
                py_mixing = torch.stack(n_samples * [py_mixing])
            for b in track(transform_batch, disable=silent):
                generative_kwargs = {"transform_batch": b}
                inference_kwargs = {"n_samples": n_samples}
                _, generative_outputs = self.module.forward(
                    tensors=tensors,
                    inference_kwargs=inference_kwargs,
                    generative_kwargs=generative_kwargs,
                    compute_loss=False,
                )
                py_mixing += torch.sigmoid(generative_outputs["py_"]["mixing"])[
                    ..., protein_mask
                ].cpu()
            py_mixing /= len(transform_batch)
            py_mixings += [py_mixing]
        if n_samples > 1:
            # concatenate along batch dimension -> result shape = (samples, cells, features)
            py_mixings = torch.cat(py_mixings, dim=1)
            # (cells, features, samples)
            py_mixings = py_mixings.permute(1, 2, 0)
        else:
            py_mixings = torch.cat(py_mixings, dim=0)

        if return_mean is True and n_samples > 1:
            py_mixings = torch.mean(py_mixings, dim=-1)

        py_mixings = py_mixings.cpu().numpy()

        if return_numpy is True:
            return 1 - py_mixings
        else:
            pro_names = self.protein_state_registry.column_names
            foreground_prob = pd.DataFrame(
                1 - py_mixings,
                columns=pro_names[protein_mask],
                index=adata.obs_names[indices],
            )
            return foreground_prob

    def _expression_for_de(
        self,
        adata=None,
        indices=None,
        n_samples_overall=None,
        transform_batch: Sequence[Number | str] | None = None,
        scale_protein=False,
        batch_size: int | None = None,
        sample_protein_mixing=False,
        include_protein_background=False,
        protein_prior_count=0.5,
        use_field: list = None,
        **kwargs,
    ):
        if use_field is None:
            use_field = ["rna", "protein"]  # Initialize the list inside the function
        if "rna" in use_field:
            gene_list = None
        else:
            gene_list = []
        if "protein" in use_field:
            protein_list = None
        else:
            protein_list = []
        rna, protein = self.get_normalized_expression(
            adata=adata,
            indices=indices,
            n_samples_overall=n_samples_overall,
            transform_batch=transform_batch,
            return_numpy=True,
            n_samples=1,
            batch_size=batch_size,
            scale_protein=scale_protein,
            sample_protein_mixing=sample_protein_mixing,
            include_protein_background=include_protein_background,
            gene_list=gene_list,
            protein_list=protein_list,
            **kwargs,
        )
        protein += protein_prior_count

        joint = np.concatenate([rna, protein], axis=1)
        return joint

    @de_dsp.dedent
    def differential_expression(
        self,
        adata: AnnData | None = None,
        groupby: str | None = None,
        group1: Iterable[str] | None = None,
        group2: str | None = None,
        idx1: Sequence[int] | Sequence[bool] | str | None = None,
        idx2: Sequence[int] | Sequence[bool] | str | None = None,
        mode: Literal["vanilla", "change"] = "change",
        delta: float = 0.25,
        batch_size: int | None = None,
        all_stats: bool = True,
        batch_correction: bool = False,
        batchid1: Iterable[str] | None = None,
        batchid2: Iterable[str] | None = None,
        fdr_target: float = 0.05,
        silent: bool = False,
        protein_prior_count: float = 0.1,
        scale_protein: bool = False,
        sample_protein_mixing: bool = False,
        include_protein_background: bool = False,
        use_field: list = None,
        pseudocounts: float | None = 1e-5,
        **kwargs,
    ) -> pd.DataFrame:
        r"""A unified method for differential expression analysis.

        Implements `"vanilla"` DE :cite:p:`Lopez18`. and `"change"` mode DE :cite:p:`Boyeau19`.

        Parameters
        ----------
        %(de_adata)s
        %(de_groupby)s
        %(de_group1)s
        %(de_group2)s
        %(de_idx1)s
        %(de_idx2)s
        %(de_mode)s
        %(de_delta)s
        %(de_batch_size)s
        %(de_all_stats)s
        %(de_batch_correction)s
        %(de_batchid1)s
        %(de_batchid2)s
        %(de_fdr_target)s
        %(de_silent)s
        protein_prior_count
            Prior count added to protein expression before LFC computation
        scale_protein
            Force protein values to sum to one in every single cell (post-hoc normalization)
        sample_protein_mixing
            Sample the protein mixture component, i.e., use the parameter to sample a Bernoulli
            that determines if expression is from foreground/background.
        include_protein_background
            Include the protein background component as part of the protein expression
        use_field
            By default uses protein and RNA field disable here to perform only RNA or protein DE.
        pseudocounts
            pseudocount offset used for the mode `change`.
            When None, observations from non-expressed genes are used to estimate its value.
        **kwargs
            Keyword args for :meth:`scvi.model.base.DifferentialComputation.get_bayes_factors`

        Returns
        -------
        Differential expression DataFrame.
        """
        if use_field is None:
            use_field = ["rna", "protein"]
        adata = self._validate_anndata(adata)
        adata_manager = self.get_anndata_manager(adata, required=True)
        model_fn = partial(
            self._expression_for_de,
            scale_protein=scale_protein,
            sample_protein_mixing=sample_protein_mixing,
            include_protein_background=include_protein_background,
            protein_prior_count=protein_prior_count,
            batch_size=batch_size,
            use_field=use_field,
        )
        all_stats_fn = partial(
            cite_seq_raw_counts_properties,
            use_field=use_field,
        )

        col_names = []
        if "rna" in use_field:
            col_names.append(np.asarray(_get_var_names_from_manager(adata_manager)))
        if "protein" in use_field:
            col_names.append(
                [str(i) + "_protein" for i in self.protein_state_registry.column_names]
            )

        col_names = np.concatenate(col_names)
        result = _de_core(
            adata_manager,
            model_fn,
            None,
            groupby,
            group1,
            group2,
            idx1,
            idx2,
            all_stats,
            all_stats_fn,
            col_names,
            mode,
            batchid1,
            batchid2,
            delta,
            batch_correction,
            fdr_target,
            silent,
            pseudocounts=pseudocounts,
            **kwargs,
        )

        return result

    @torch.inference_mode()
    def posterior_predictive_sample(
        self,
        adata: AnnOrMuData | None = None,
        indices: Sequence[int] | None = None,
        n_samples: int = 1,
        batch_size: int | None = None,
        gene_list: Sequence[str] | None = None,
        protein_list: Sequence[str] | None = None,
    ) -> np.ndarray:
        r"""Generate observation samples from the posterior predictive distribution.

        The posterior predictive distribution is written as :math:`p(\hat{x}, \hat{y} \mid x, y)`.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        n_samples
            Number of required samples for each cell
        batch_size
            Minibatch size for data loading into model. Defaults to `scvi.settings.batch_size`.
        gene_list
            Names of genes of interest
        protein_list
            Names of proteins of interest

        Returns
        -------
        x_new : :class:`~numpy.ndarray`
            tensor with shape (n_cells, n_genes, n_samples)
        """
        if self.module.gene_likelihood not in ["nb"]:
            raise ValueError("Invalid gene_likelihood")

        adata = self._validate_anndata(adata)
        adata_manager = self.get_anndata_manager(adata, required=True)
        if gene_list is None:
            gene_mask = slice(None)
        else:
            all_genes = _get_var_names_from_manager(adata_manager)
            gene_mask = [True if gene in gene_list else False for gene in all_genes]
        if protein_list is None:
            protein_mask = slice(None)
        else:
            all_proteins = self.protein_state_registry.column_names
            protein_mask = [True if p in protein_list else False for p in all_proteins]

        scdl = self._make_data_loader(adata=adata, indices=indices, batch_size=batch_size)

        rna_list = []
        protein_list = []
        for tensors in scdl:
            rna_sample, protein_sample = self.module.sample(tensors, n_samples=n_samples)
            rna_sample = rna_sample[..., gene_mask]
            protein_sample = protein_sample[..., protein_mask]

            rna_list += [rna_sample]
            protein_list += [protein_sample]
            if n_samples > 1:
                rna_list[-1] = np.transpose(rna_list[-1], (1, 2, 0))
                protein_list[-1] = np.transpose(protein_list[-1], (1, 2, 0))
        rna = np.concatenate(rna_list, axis=0)
        protein = np.concatenate(protein_list, axis=0)

        if isinstance(adata, AnnData):
            return {"rna": rna, "protein": protein}
        else:
            return {self.modalities["rna_layer"]: rna, self.modalities["protein_layer"]: protein}

    @torch.inference_mode()
    def _get_denoised_samples(
        self,
        adata=None,
        indices=None,
        n_samples: int = 25,
        batch_size: int = 64,
        rna_size_factor: int = 1000,
        transform_batch: int | None = None,
    ) -> np.ndarray:
        """Return samples from an adjusted posterior predictive.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            indices of `adata` to use
        n_samples
            How may samples per cell
        batch_size
            Minibatch size for data loading into model. Defaults to `scvi.settings.batch_size`.
        rna_size_factor
            size factor for RNA prior to sampling gamma distribution
        transform_batch
            int of which batch to condition on for all cells
        """
        adata = self._validate_anndata(adata)
        scdl = self._make_data_loader(adata=adata, indices=indices, batch_size=batch_size)

        scdl_list = []
        for tensors in scdl:
            x = tensors[REGISTRY_KEYS.X_KEY]
            y = tensors[REGISTRY_KEYS.PROTEIN_EXP_KEY]

            generative_kwargs = {"transform_batch": transform_batch}
            inference_kwargs = {"n_samples": n_samples}
            with torch.inference_mode():
                (
                    inference_outputs,
                    generative_outputs,
                ) = self.module.forward(
                    tensors,
                    inference_kwargs=inference_kwargs,
                    generative_kwargs=generative_kwargs,
                    compute_loss=False,
                )
            px_ = generative_outputs["px_"]
            py_ = generative_outputs["py_"]
            device = px_["r"].device

            pi = 1 / (1 + torch.exp(-py_["mixing"]))
            mixing_sample = torch.distributions.Bernoulli(pi).sample()
            protein_rate = py_["rate_fore"]
            rate = torch.cat((rna_size_factor * px_["scale"], protein_rate), dim=-1)
            if len(px_["r"].size()) == 2:
                px_dispersion = px_["r"]
            else:
                px_dispersion = torch.ones_like(x).to(device) * px_["r"]
            if len(py_["r"].size()) == 2:
                py_dispersion = py_["r"]
            else:
                py_dispersion = torch.ones_like(y).to(device) * py_["r"]

            dispersion = torch.cat((px_dispersion, py_dispersion), dim=-1)

            # This gamma is really l*w using scVI manuscript notation
            p = rate / (rate + dispersion)
            r = dispersion
            # TODO: NEED TORCH MPS FIX for 'aten::_standard_gamma'
            l_train = (
                torch.distributions.Gamma(r.to("cpu"), ((1 - p) / p).to("cpu")).sample().to("mps")
                if self.device.type == "mps"
                else torch.distributions.Gamma(r, (1 - p) / p).sample()
            )
            data = l_train.cpu().numpy()
            # make background 0
            data[:, :, x.shape[1] :] = data[:, :, x.shape[1] :] * (1 - mixing_sample).cpu().numpy()
            scdl_list += [data]

            scdl_list[-1] = np.transpose(scdl_list[-1], (1, 2, 0))

        return np.concatenate(scdl_list, axis=0)

    @torch.inference_mode()
    def get_feature_correlation_matrix(
        self,
        adata=None,
        indices=None,
        n_samples: int = 10,
        batch_size: int = 64,
        rna_size_factor: int = 1000,
        transform_batch: Sequence[Number | str] | None = None,
        correlation_type: Literal["spearman", "pearson"] = "spearman",
        log_transform: bool = False,
        silent: bool = True,
    ) -> pd.DataFrame:
        """Generate gene-gene correlation matrix using scvi uncertainty and expression.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        n_samples
            Number of posterior samples to use for estimation.
        batch_size
            Minibatch size for data loading into model. Defaults to `scvi.settings.batch_size`.
        rna_size_factor
            size factor for RNA prior to sampling gamma distribution
        transform_batch
            Batches to condition on.
            If transform_batch is:

            - None, then real observed batch is used
            - int, then batch transform_batch is used
            - list of int, then values are averaged over provided batches.
        correlation_type
            One of "pearson", "spearman".
        log_transform
            Whether to log transform denoised values prior to correlation calculation.
        %(de_silent)s

        Returns
        -------
        Gene-protein-gene-protein correlation matrix
        """
        from scipy.stats import spearmanr

        adata = self._validate_anndata(adata)
        adata_manager = self.get_anndata_manager(adata, required=True)

        if not isinstance(transform_batch, IterableClass):
            transform_batch = [transform_batch]

        transform_batch = _get_batch_code_from_category(
            self.get_anndata_manager(adata, required=True), transform_batch
        )

        corr_mats = []
        for b in track(transform_batch, disable=silent):
            denoised_data = self._get_denoised_samples(
                n_samples=n_samples,
                batch_size=batch_size,
                rna_size_factor=rna_size_factor,
                transform_batch=b,
                indices=indices,
            )
            flattened = np.zeros((denoised_data.shape[0] * n_samples, denoised_data.shape[1]))
            for i in range(n_samples):
                flattened[denoised_data.shape[0] * (i) : denoised_data.shape[0] * (i + 1)] = (
                    denoised_data[:, :, i]
                )
            if log_transform is True:
                flattened[:, : self.n_genes] = np.log(flattened[:, : self.n_genes] + 1e-8)
                flattened[:, self.n_genes :] = np.log1p(flattened[:, self.n_genes :])
            if correlation_type == "pearson":
                corr_matrix = np.corrcoef(flattened, rowvar=False)
            else:
                corr_matrix, _ = spearmanr(flattened, axis=0)
            corr_mats.append(corr_matrix)

        corr_matrix = np.mean(np.stack(corr_mats), axis=0)
        var_names = _get_var_names_from_manager(adata_manager)
        names = np.concatenate(
            [
                np.asarray(var_names),
                self.protein_state_registry.column_names,
            ]
        )
        return pd.DataFrame(corr_matrix, index=names, columns=names)

    @torch.inference_mode()
    def get_likelihood_parameters(
        self,
        adata: AnnData | None = None,
        indices: Sequence[int] | None = None,
        n_samples: int | None = 1,
        give_mean: bool | None = False,
        batch_size: int | None = None,
    ) -> dict[str, np.ndarray]:
        r"""Estimates for the parameters of the likelihood :math:`p(x, y \mid z)`.

        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        n_samples
            Number of posterior samples to use for estimation.
        give_mean
            Return expected value of parameters or a samples
        batch_size
            Minibatch size for data loading into model. Defaults to `scvi.settings.batch_size`.
        """
        raise NotImplementedError

    def _validate_anndata(self, adata: AnnData | None = None, copy_if_view: bool = True):
        adata = super()._validate_anndata(adata=adata, copy_if_view=copy_if_view)
        error_msg = (
            "Number of {} in anndata different from when setup_anndata was run. Please rerun "
            "setup_anndata."
        )
        if REGISTRY_KEYS.PROTEIN_EXP_KEY in self.adata_manager.data_registry.keys():
            pro_exp = self.get_from_registry(adata, REGISTRY_KEYS.PROTEIN_EXP_KEY)
            if self.summary_stats.n_proteins != pro_exp.shape[1]:
                raise ValueError(error_msg.format("proteins"))
            is_nonneg_int = _check_nonnegative_integers(pro_exp)
            if not is_nonneg_int:
                warnings.warn(
                    "Make sure the registered protein expression in anndata contains "
                    "unnormalized count data.",
                    UserWarning,
                    stacklevel=settings.warnings_stacklevel,
                )
        else:
            raise ValueError("No protein data found, please setup or transfer anndata")

        return adata

    def _get_totalvi_protein_priors(self, adata, n_cells=100):
        """Compute an empirical prior for protein background."""
        from sklearn.exceptions import ConvergenceWarning
        from sklearn.mixture import GaussianMixture

        with warnings.catch_warnings():
            warnings.filterwarnings("error")
            logger.info("Computing empirical prior initialization for protein background.")

            adata = self._validate_anndata(adata)
            adata_manager = self.get_anndata_manager(adata)
            pro_exp = adata_manager.get_from_registry(REGISTRY_KEYS.PROTEIN_EXP_KEY)
            pro_exp = pro_exp.to_numpy() if isinstance(pro_exp, pd.DataFrame) else pro_exp
            batch_mask = adata_manager.get_state_registry(REGISTRY_KEYS.PROTEIN_EXP_KEY).get(
                fields.ProteinObsmField.PROTEIN_BATCH_MASK
            )
            if "n_panel" in self.summary_stats:
                batch = adata_manager.get_from_registry("panel").ravel()
                cats = adata_manager.get_state_registry("panel")[
                    fields.CategoricalObsField.CATEGORICAL_MAPPING_KEY
                ]
            else:
                batch = adata_manager.get_from_registry(REGISTRY_KEYS.BATCH_KEY).ravel()
                cats = adata_manager.get_state_registry(REGISTRY_KEYS.BATCH_KEY)[
                    fields.CategoricalObsField.CATEGORICAL_MAPPING_KEY
                ]
            codes = np.arange(len(cats))

            batch_avg_mus, batch_avg_scales = [], []
            for b in np.unique(codes):
                # can happen during online updates
                # the values of these batches will not be used
                num_in_batch = np.sum(batch == b)
                if num_in_batch == 0:
                    batch_avg_mus.append(0)
                    batch_avg_scales.append(1)
                    continue
                batch_pro_exp = pro_exp[batch == b]

                # non missing
                if batch_mask is not None:
                    batch_pro_exp = batch_pro_exp[:, batch_mask[str(b)]]
                    if batch_pro_exp.shape[1] < 5:
                        logger.debug(
                            f"Batch {b} has too few proteins to set prior, setting randomly."
                        )
                        batch_avg_mus.append(0.0)
                        batch_avg_scales.append(0.05)
                        continue

                # a batch is missing because it's in the reference but not query data
                # for scarches case, these values will be replaced by original state dict
                if batch_pro_exp.shape[0] == 0:
                    batch_avg_mus.append(0.0)
                    batch_avg_scales.append(0.05)
                    continue

                cells = np.random.choice(np.arange(batch_pro_exp.shape[0]), size=n_cells)
                batch_pro_exp = batch_pro_exp[cells]
                gmm = GaussianMixture(n_components=2)
                mus, scales = [], []
                # fit per cell GMM
                for c in batch_pro_exp:
                    try:
                        gmm.fit(np.log1p(c.reshape(-1, 1)))
                    # when cell is all 0
                    except ConvergenceWarning:
                        mus.append(0)
                        scales.append(0.05)
                        continue

                    means = gmm.means_.ravel()
                    sorted_fg_bg = np.argsort(means)
                    mu = means[sorted_fg_bg].ravel()[0]
                    covariances = gmm.covariances_[sorted_fg_bg].ravel()[0]
                    scale = np.sqrt(covariances)
                    mus.append(mu)
                    scales.append(scale)

                # average distribution over cells
                batch_avg_mu = np.mean(mus)
                batch_avg_scale = np.sqrt(np.sum(np.square(scales)) / (n_cells**2))

                batch_avg_mus.append(batch_avg_mu)
                batch_avg_scales.append(batch_avg_scale)

            # repeat prior for each protein
            batch_avg_mus = np.array(batch_avg_mus, dtype=np.float32).reshape(1, -1)
            batch_avg_scales = np.array(batch_avg_scales, dtype=np.float32).reshape(1, -1)
            batch_avg_mus = np.tile(batch_avg_mus, (pro_exp.shape[1], 1))
            batch_avg_scales = np.tile(batch_avg_scales, (pro_exp.shape[1], 1))

        return batch_avg_mus, batch_avg_scales

    @torch.inference_mode()
    def get_protein_background_mean(self, adata, indices, batch_size):
        """Get protein background mean."""
        adata = self._validate_anndata(adata)
        scdl = self._make_data_loader(adata=adata, indices=indices, batch_size=batch_size)
        background_mean = []
        for tensors in scdl:
            _, inference_outputs, _ = self.module.forward(tensors)
            b_mean = inference_outputs["py_"]["rate_back"]
            background_mean += [b_mean.cpu().numpy()]
        return np.concatenate(background_mean)

    @classmethod
    @setup_anndata_dsp.dedent
    def setup_anndata(
        cls,
        adata: AnnData,
        protein_expression_obsm_key: str,
        protein_names_uns_key: str | None = None,
        batch_key: str | None = None,
        panel_key: str | None = None,
        layer: str | None = None,
        size_factor_key: str | None = None,
        categorical_covariate_keys: list[str] | None = None,
        continuous_covariate_keys: list[str] | None = None,
        **kwargs,
    ):
        """%(summary)s.

        Parameters
        ----------
        %(param_adata)s
        protein_expression_obsm_key
            key in `adata.obsm` for protein expression data.
        protein_names_uns_key
            key in `adata.uns` for protein names. If None, will use the column names of
            `adata.obsm[protein_expression_obsm_key]` if it is a DataFrame, else will assign
            sequential names to proteins.
        %(param_batch_key)s
        panel_key
            key in 'adata.obs' for the various panels used to measure proteins.
        %(param_layer)s
        %(param_size_factor_key)s
        %(param_cat_cov_keys)s
        %(param_cont_cov_keys)s

        Returns
        -------
        %(returns)s
        """
        warnings.warn(
            "We recommend using setup_mudata for multi-modal data."
            "It does not influence model performance",
            DeprecationWarning,
            stacklevel=settings.warnings_stacklevel,
        )
        setup_method_args = cls._get_setup_method_args(**locals())
        if panel_key is not None:
            batch_field = fields.CategoricalObsField("panel", panel_key)
        else:
            batch_field = fields.CategoricalObsField(REGISTRY_KEYS.BATCH_KEY, batch_key)
        anndata_fields = [
            fields.LayerField(REGISTRY_KEYS.X_KEY, layer, is_count_data=True),
            fields.CategoricalObsField(
                REGISTRY_KEYS.LABELS_KEY, None
            ),  # Default labels field for compatibility with TOTALVAE
            fields.CategoricalObsField(REGISTRY_KEYS.BATCH_KEY, batch_key),
            fields.NumericalObsField(
                REGISTRY_KEYS.SIZE_FACTOR_KEY, size_factor_key, required=False
            ),
            fields.CategoricalJointObsField(
                REGISTRY_KEYS.CAT_COVS_KEY, categorical_covariate_keys
            ),
            fields.NumericalJointObsField(REGISTRY_KEYS.CONT_COVS_KEY, continuous_covariate_keys),
            fields.ProteinObsmField(
                REGISTRY_KEYS.PROTEIN_EXP_KEY,
                protein_expression_obsm_key,
                use_batch_mask=True,
                batch_field=batch_field,
                colnames_uns_key=protein_names_uns_key,
                is_count_data=True,
            ),
        ]
        if panel_key is not None:
            anndata_fields.insert(0, fields.CategoricalObsField("panel", panel_key))

        adata_manager = AnnDataManager(fields=anndata_fields, setup_method_args=setup_method_args)
        adata_manager.register_fields(adata, **kwargs)
        cls.register_manager(adata_manager)

    @classmethod
    @setup_anndata_dsp.dedent
    def setup_mudata(
        cls,
        mdata: MuData,
        rna_layer: str | None = None,
        protein_layer: str | None = None,
        batch_key: str | None = None,
        panel_key: str | None = None,
        size_factor_key: str | None = None,
        categorical_covariate_keys: list[str] | None = None,
        continuous_covariate_keys: list[str] | None = None,
        modalities: dict[str, str] | None = None,
        **kwargs,
    ):
        """%(summary_mdata)s.

        Parameters
        ----------
        %(param_mdata)s
        rna_layer
            RNA layer key. If `None`, will use `.X` of specified modality key.
        protein_layer
            Protein layer key. If `None`, will use `.X` of specified modality key.
        %(param_batch_key)s
        panel_key
            key in 'adata.obs' for the various panels used to measure proteins.
        %(param_size_factor_key)s
        %(param_cat_cov_keys)s
        %(param_cont_cov_keys)s
        %(param_modalities)s

        Examples
        --------
        >>> mdata = muon.read_10x_h5("pbmc_10k_protein_v3_filtered_feature_bc_matrix.h5")
        >>> scvi.model.TOTALVI.setup_mudata(
                mdata, modalities={"rna_layer": "rna", "protein_layer": "prot"}
            )
        >>> vae = scvi.model.TOTALVI(mdata)
        """
        setup_method_args = cls._get_setup_method_args(**locals())

        if modalities is None:
            raise ValueError("Modalities cannot be None.")
        modalities = cls._create_modalities_attr_dict(modalities, setup_method_args)

        if panel_key is not None:
            batch_field = fields.MuDataCategoricalObsField(
                "panel",
                panel_key,
                mod_key=modalities.batch_key,
            )
        else:
            batch_field = fields.MuDataCategoricalObsField(
                REGISTRY_KEYS.BATCH_KEY,
                batch_key,
                mod_key=modalities.batch_key,
            )

        mudata_fields = [
            fields.MuDataLayerField(
                REGISTRY_KEYS.X_KEY,
                rna_layer,
                mod_key=modalities.rna_layer,
                is_count_data=True,
                mod_required=True,
            ),
            fields.MuDataCategoricalObsField(
                REGISTRY_KEYS.LABELS_KEY,
                None,
                mod_key=None,
            ),  # Default labels field for compatibility with TOTALVAE
            fields.MuDataCategoricalObsField(
                REGISTRY_KEYS.BATCH_KEY,
                batch_key,
                mod_key=modalities.batch_key,
            ),
            fields.MuDataNumericalObsField(
                REGISTRY_KEYS.SIZE_FACTOR_KEY,
                size_factor_key,
                mod_key=modalities.size_factor_key,
                required=False,
            ),
            fields.MuDataCategoricalJointObsField(
                REGISTRY_KEYS.CAT_COVS_KEY,
                categorical_covariate_keys,
                mod_key=modalities.categorical_covariate_keys,
            ),
            fields.MuDataNumericalJointObsField(
                REGISTRY_KEYS.CONT_COVS_KEY,
                continuous_covariate_keys,
                mod_key=modalities.continuous_covariate_keys,
            ),
            fields.MuDataProteinLayerField(
                REGISTRY_KEYS.PROTEIN_EXP_KEY,
                protein_layer,
                mod_key=modalities.protein_layer,
                use_batch_mask=True,
                batch_field=batch_field,
                is_count_data=True,
                mod_required=True,
            ),
        ]

        if panel_key:
            mudata_fields.insert(
                0,
                fields.MuDataCategoricalObsField(
                    "panel",
                    panel_key,
                    mod_key=modalities.batch_key,
                ),
            )

        mdata_minify_type = _get_adata_minify_type(mdata)
        if mdata_minify_type is not None:
            mudata_fields += cls._get_fields_for_mudata_minification(mdata_minify_type)

        adata_manager = AnnDataManager(fields=mudata_fields, setup_method_args=setup_method_args)
        adata_manager.register_fields(mdata, **kwargs)
        cls.register_manager(adata_manager)
