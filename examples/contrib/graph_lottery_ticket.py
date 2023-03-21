import functools
from argparse import ArgumentParser

import torch
import tqdm
from torch.nn import Module
from torch.nn.functional import nll_loss
from torch.nn.init import trunc_normal_

import torch_geometric
from torch_geometric.contrib.nn import GLTModel, GLTSearch
from torch_geometric.contrib.nn.models.graph_lottery_ticket import score_node_classification, score_link_prediction
from torch_geometric.data import Data
from torch_geometric.datasets import Planetoid
from torch_geometric.nn import GAT, GIN, GCN

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

EDGE_MASK = GLTModel.EDGE_MASK + GLTModel.MASK
INIT_FUNC = functools.partial(trunc_normal_, mean=1, a=1 - 1e-3, b=1 + 1e-3)


def generate_edge_data(dataset):
    negative_edges = torch_geometric.utils.negative_sampling(dataset.edge_index)
    edge_labels = [0] * negative_edges.shape[-1] + [1] * dataset.edge_index.shape[-1]
    dataset.edges = torch.cat([dataset.edge_index, negative_edges], dim=-1)
    dataset.edge_labels = torch.tensor(edge_labels, device=device)


class LinkPredictor(Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, x, edge_index, edge_weight, edges):
        x = self.model(x, edge_index, edge_weight=edge_weight)
        edge_feat_i = x[edges[0]]
        edge_feat_j = x[edges[1]]
        return (edge_feat_i * edge_feat_j).sum(dim=-1)


def baseline(model: Module, graph: Data, task, verbose: bool = False):
    initial_params = model.state_dict()
    best_val_score = 0.0
    final_test_score = 0.0
    optimizer = torch.optim.Adam(model.parameters(), lr=8e-3, weight_decay=8e-5)
    if task == "link_prediction":
        loss_fn = torch.nn.functional.binary_cross_entropy_with_logits
    else:
        loss_fn = torch.nn.functional.cross_entropy

    with tqdm.trange(200, disable=not verbose) as t:
        for epoch in t:
            model.train()
            optimizer.zero_grad()

            if task == "node_classification":
                output = model(graph.x, graph.edge_index, edge_weight=graph.edge_weight)

                loss = loss_fn(
                    output[graph.train_mask], graph.y[graph.train_mask]
                )
            elif task == "link_prediction":
                output = model(graph.x, graph.edge_index, edge_weight=graph.edge_weight, edges=graph.edges)

                edge_mask = graph.train_mask[graph.edges[0]] & graph.train_mask[graph.edges[1]]
                loss = loss_fn(
                    output[edge_mask], graph.edge_labels[edge_mask].float()
                )
            else:
                raise ValueError(f"{task} must be one of node class. or link pred.")

            loss.backward()
            optimizer.step()

            model.eval()
            if task == "node_classification":
                preds = model(graph.x, graph.edge_index, edge_weight=graph.edge_weight).argmax(dim=1)
                val_score, test_score = score_node_classification(graph.y, preds, graph.val_mask, graph.test_mask)
            elif task == "link_prediction":
                preds = model(graph.x, graph.edge_index, edge_weight=graph.edge_weight, edges=graph.edges)
                val_mask = graph.val_mask[graph.edges[0]] & graph.val_mask[graph.edges[1]]
                test_mask = graph.test_mask[graph.edges[0]] & graph.test_mask[graph.edges[1]]
                val_score, test_score = score_link_prediction(graph.edge_labels, preds, val_mask, test_mask)
            else:
                raise ValueError(f"{task} must be one of node class. or link pred.")

            if val_score > best_val_score:
                best_val_score = val_score
                final_test_score = test_score

            t.set_postfix(
                {"loss": loss.item(), "val_score": val_score, "test_score": test_score}
            )
    model.load_state_dict(initial_params)
    print("[BASELINE] Final test performance:", final_test_score)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument('-d', "--dataset", default="Cora")
    parser.add_argument('-m', "--model", default="gcn")
    parser.add_argument("--reg_graph", default=.001, type=float)
    parser.add_argument("--reg_model", default=.001, type=float)
    parser.add_argument("--prune_rate_graph", default=.05, type=float)
    parser.add_argument("--prune_rate_model", default=.8, type=float)
    parser.add_argument("--task", )
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    dataset = Planetoid(root=f"tmp/{args.dataset}", name=args.dataset)
    print(args.dataset)
    print(args.model)
    data = dataset[0].to(device)
    if args.task == "link_prediction":
        generate_edge_data(data)

    print(f"Nodes: {data.num_nodes}")
    print(f"Edges: {data.num_edges}")

    hidden_channels = 512
    if args.model.lower() == "gcn":
        gnn = GCN(
            in_channels=dataset.num_node_features,
            output_channels=hidden_channels,
            hidden_channels=hidden_channels,
            num_layers=2
        ).to(device)

    elif args.model.lower() == "gin":
        gnn = GIN(
            in_channels=dataset.num_node_features,
            output_channels=hidden_channels,
            hidden_channels=hidden_channels,
            num_layers=2
        ).to(device)

    elif args.model.lower() == "gat":
        gnn = GAT(
            in_channels=dataset.num_node_features,
            output_channels=hidden_channels,
            hidden_channels=hidden_channels,
            num_layers=2
        ).to(device)

    else:
        raise ValueError("model must be one of gcn, gin, gat")

    if args.task == "link_prediction":
        gnn = LinkPredictor(gnn)

    # baseline(gnn, data, args.task, args.verbose)

    if args.task == "link_prediction":
        loss_fn = torch.nn.functional.binary_cross_entropy_with_logits
    else:
        loss_fn = torch.nn.functional.cross_entropy

    trainer = GLTSearch(
        task=args.task,
        module=gnn,
        graph=data,
        lr=8e-3,
        reg_graph=args.reg_graph,
        reg_model=args.reg_model,
        prune_rate_graph=args.prune_rate_graph,
        prune_rate_model=args.prune_rate_model,
        optim_args={"weight_decay": 8e-5},
        seed=1234,
        verbose=args.verbose,
        ignore_keys={"eps"},
        max_train_epochs=200,
        loss_fn=loss_fn
    )

    initial_params, mask_dict = trainer.prune()
