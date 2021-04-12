from typing import Any, Dict, Sequence, Union
import logging

import numpy as np
import torch
from sacred import Experiment

from rgnn_at_scale.data import prep_graph, split
from rgnn_at_scale.helper.io import Storage


def prepare_global_attack_experiment(data_dir: str, dataset: str, attack: str, attack_params: Dict[str, Any],
                                     epsilons: Sequence[float], binary_attr: bool, make_undirected: bool,
                                     make_unweighted: bool,  normalize: bool, normalize_attr: str, seed: int,
                                     artifact_dir: str, pert_adj_storage_type: str, pert_attr_storage_type: str,
                                     model_label: str, model_storage_type: str, device: Union[str, int],
                                     surrogate_model_label: str, data_device: Union[str, int], ex: Experiment):
    logging.info({
        'dataset': dataset, 'attack': attack, 'attack_params': attack_params, 'epsilons': epsilons,
        'make_undirected': make_undirected, 'make_unweighted': make_unweighted, 'normalize': normalize,
        'normalize_attr': normalize_attr, 'binary_attr': binary_attr,
        'seed': seed, 'artifact_dir': artifact_dir, 'pert_adj_storage_type': pert_adj_storage_type,
        'pert_attr_storage_type': pert_attr_storage_type, 'model_label': model_label,
        'model_storage_type': model_storage_type, 'device': device
    })

    assert sorted(epsilons) == epsilons, 'argument `epsilons` must be a sorted list'
    assert len(np.unique(epsilons)) == len(epsilons),\
        'argument `epsilons` must be unique (strictly increasing)'
    assert all([eps >= 0 for eps in epsilons]), 'all elements in `epsilons` must be greater than 0'

    # To increase consistency between runs
    torch.manual_seed(seed)
    np.random.seed(seed)

    graph = prep_graph(dataset, data_device, dataset_root=data_dir,
                       normalize=normalize,
                       normalize_attr=normalize_attr,
                       make_undirected=make_undirected,
                       make_unweighted=make_unweighted,
                       binary_attr=binary_attr,
                       return_original_split=dataset.startswith('ogbn'))

    attr, adj, labels = graph[:3]
    if len(graph) == 3:
        idx_train, idx_val, idx_test = split(labels.cpu().numpy())
    else:
        idx_train, idx_val, idx_test = graph[3]['train'], graph[3]['valid'], graph[3]['test']

    storage = Storage(artifact_dir, experiment=ex)

    pert_params = dict(dataset=dataset,
                       binary_attr=binary_attr,
                       normalize=normalize,
                       normalize_attr=normalize_attr,
                       make_undirected=make_undirected,
                       make_unweighted=make_unweighted,
                       seed=seed,
                       attack=attack,
                       model=model_label,
                       surrogate_model=surrogate_model_label,
                       attack_params=attack_params)

    model_params = dict(dataset=dataset,
                        binary_attr=binary_attr,
                        normalize=normalize,
                        normalize_attr=normalize_attr,
                        make_undirected=make_undirected,
                        make_unweighted=make_unweighted,
                        label=model_label,
                        seed=seed)

    if make_undirected:
        m = adj.nnz() / 2
    else:
        m = adj.nnz()

    return attr, adj, labels, idx_train, idx_val, idx_test,  storage, pert_params, model_params, m


def run_global_attack(epsilon, m, storage, pert_adj_storage_type, pert_attr_storage_type,
                      pert_params, adversary, model_label):
    n_perturbations = int(round(epsilon * m))

    pert_adj = storage.load_artifact(pert_adj_storage_type, {**pert_params, **{'epsilon': epsilon}})
    pert_attr = storage.load_artifact(pert_attr_storage_type, {**pert_params, **{'epsilon': epsilon}})

    if pert_adj is not None and pert_attr is not None:
        logging.info(
            f"Found cached perturbed adjacency and attribute matrix for model '{model_label}' and eps {epsilon}")
        adversary.set_pertubations(pert_adj, pert_attr)
    else:
        logging.info(
            f"No cached perturbations found for model '{model_label}' and eps {epsilon}. Execute attack...")
        adversary.attack(n_perturbations)
        pert_adj, pert_attr = adversary.get_pertubations()

        storage.save_artifact(pert_adj_storage_type, {**pert_params, **{'epsilon': epsilon}}, pert_adj)
        storage.save_artifact(pert_attr_storage_type, {**pert_params, **{'epsilon': epsilon}}, pert_attr)
