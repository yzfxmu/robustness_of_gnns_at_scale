import logging
import warnings
from typing import Any, Dict, Sequence, Union

import numpy as np
from sacred import Experiment
import seml

from rgnn_at_scale.attacks import create_attack
from rgnn_at_scale.helper.io import Storage
from experiments.common import prepare_attack_experiment, get_local_attack_nodes

ex = Experiment()
seml.setup_logger(ex)


@ex.post_run_hook
def collect_stats(_run):
    seml.collect_exp_stats(_run)


@ex.config
def config():
    overwrite = None
    db_collection = None
    if db_collection is not None:
        ex.observers.append(seml.create_mongodb_observer(db_collection, overwrite=overwrite))

    # default params
    dataset = 'cora_ml'  # Options are 'cora_ml' and 'citeseer' (or with a big GPU 'pubmed')
    attack = 'Nettack'
    attack_params = {}
    nodes = None  # [1854, 513, 2383]

    epsilons = [0.5, 0.75, 1]
    seed = 0

    artifact_dir = "/nfs/students/schmidtt/cache/cache"

    model_storage_type = 'victim_cora'
    model_label = None

    surrogate_model_storage_type = "surrogate_cora"
    surrogate_model_label = 'Linear GCN'

    data_dir = "/nfs/students/schmidtt/datasets/"
    binary_attr = False
    make_undirected = True

    data_device = 'cpu'
    device = "cpu"
    debug_level = "info"


@ex.automain
def run(data_dir: str, dataset: str, attack: str, attack_params: Dict[str, Any], nodes: str, seed: int,
        epsilons: Sequence[float], binary_attr: bool, make_undirected: bool, artifact_dir: str,
        model_label: str, model_storage_type: str, device: Union[str, int], surrogate_model_storage_type: str,
        surrogate_model_label: str, data_device: Union[str, int], debug_level: str):

    assert sorted(epsilons) == epsilons, 'argument `epsilons` must be a sorted list'
    assert len(np.unique(epsilons)) == len(epsilons),\
        'argument `epsilons` must be unique (strictly increasing)'
    assert all([eps >= 0 for eps in epsilons]), 'all elements in `epsilons` must be greater than 0'
    results = []

    (
        attr, adj, labels, _, _, idx_test, storage, attack_params, _, model_params, _
    ) = prepare_attack_experiment(
        data_dir, dataset, attack, attack_params, epsilons, binary_attr, make_undirected, seed, artifact_dir,
        None, None, model_label, model_storage_type, device, surrogate_model_label, data_device, debug_level, debug_level, ex
    )

    storage = Storage(artifact_dir, experiment=ex)

    models_and_hyperparams = storage.find_models(model_storage_type, model_params)

    model_params["label"] = surrogate_model_label
    surrogate_models_and_hyperparams = storage.find_models(surrogate_model_storage_type, model_params)

    assert len(surrogate_models_and_hyperparams) > 0, "No surrogate model found!"
    if len(surrogate_models_and_hyperparams) > 1:
        warnings.warn("More than one matching surrogate model found. Choose last one by default.")

    surrogate_model = surrogate_models_and_hyperparams[-1][0]

    tmp_nodes = np.array(nodes)
    if nodes is None:
        tmp_nodes = get_local_attack_nodes(attack, binary_attr, attr, adj, labels,
                                           surrogate_model, idx_test, device, attack_params, topk=10)

    tmp_nodes = [int(i) for i in tmp_nodes]
    for node in tmp_nodes:
        degree = adj[node].sum()

        tmp_epsilons = list(epsilons)
        #tmp_epsilons.insert(0, 0)
        for eps in tmp_epsilons:
            try:
                adversary = create_attack(attack, attr=attr, adj=adj, labels=labels, model=surrogate_model,
                                          idx_attack=idx_test, device=device,  data_device=data_device,
                                          binary_attr=binary_attr, make_undirected=make_undirected, **attack_params)
            except Exception as e:
                logging.exception(e)
                logging.error(f"Failed to instantiate attack {attack} for model '{surrogate_model}'.")
                continue

            n_perturbations = int((eps * degree).round().item())

            # In case the model is non-deterministic to get the results either after attacking or after loading
            try:
                adversary.attack(n_perturbations, node_idx=node)
            except Exception as e:
                logging.exception(e)
                logging.error(
                    f"Failed to attack model '{surrogate_model}' using {attack} with eps {eps} at node {node}.")
                continue

            for model, hyperparams in models_and_hyperparams:
                try:
                    eval_model_label = hyperparams['label']

                    adversary.set_eval_model(model)
                    logits, initial_logits = adversary.evaluate_local(node)

                    logging.info(
                        f'Evaluated model {eval_model_label} using {attack} with pert. edges for node {node} and budget {n_perturbations}: {adversary.get_perturbed_edges()}')

                    results.append({
                        'label': eval_model_label,
                        'epsilon': eps,
                        'n_perturbations': n_perturbations,
                        'degree': int(degree.item()),
                        'logits': logits.cpu(),
                        'initial_logits': initial_logits.cpu(),
                        'target': labels[node].item(),
                        'node_id': node,
                        'perturbed_edges': adversary.get_perturbed_edges().cpu().numpy()
                    })

                    results[-1].update(adversary.classification_statistics(logits.cpu(), labels[node].long().cpu()))
                    results[-1].update({
                        f'initial_{key}': value
                        for key, value
                        in adversary.classification_statistics(initial_logits.cpu(), labels[node].long().cpu()).items()
                    })
                except Exception as e:
                    logging.exception(e)
                    logging.error(
                        f"Failed to evaluate model '{eval_model_label}' using {attack} with eps {eps} at node {node}.")
                    continue
                # if hasattr(adversary, 'attack_statistics'):
                #     results[-1]['attack_statistics'] = adversary.attack_statistics

    assert len(results) > 0

    return {
        'results': results
    }
