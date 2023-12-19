import torch_geometric.nn as geom_nn
import torch_geometric.data as geom_data
import torch.nn as nn
import pytorch_lightning as pl
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint
import torch
from torch_geometric.data import Data
import torch.optim as optim
import pandas as pd
import os
from sklearn.model_selection import train_test_split
import numpy as np
import graph_tool.all as gt
from tqdm import tqdm

CHECKPOINT_PATH = "../saved_models/"


device = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
print(device)

gnn_layer_by_name = {
    "GCN": geom_nn.GCNConv,
    "GAT": geom_nn.GATConv,
    "GraphConv": geom_nn.GraphConv
}

from torch_geometric.data import InMemoryDataset, Data

class SingleObjectDataset(InMemoryDataset):
    def __init__(self, data):
        super(SingleObjectDataset, self).__init__()
        self.data = data

        # Process the single data object
        self.data_list = [data]

    def __len__(self):
        return 1  # Only one object in the dataset

    def get(self, idx):
        return self.data_list[idx]

# Create a dataset with a single object

# Example graph data
# %%
def gt_to_pytorch_geometric(g, alltrials_categorical_df, target_variable="phase"):
    # plausible target variables: "phase", "overall_status"
    # Assuming you have a dataset with node features (x) and target labels (y)
    # Split the dataset into train, validation, and test sets
    alltrials_categorical_df.fillna("NA", inplace=True)
    # convert all variabls to numeric
    for col in alltrials_categorical_df.columns:
        alltrials_categorical_df[col] = pd.factorize(alltrials_categorical_df[col])[0]
    # %%
    x = torch.from_numpy(np.array(alltrials_categorical_df.drop(target_variable, axis=1), dtype = np.float32))  # Node features (random for illustration)
    
    y = torch.tensor(alltrials_categorical_df[target_variable])  # Target labels (for supervised learning)

    x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.5)
    x_train, x_val, y_train, y_val = train_test_split(x_train, y_train, test_size=0.5)

    # Create boolean masks for each split
    train_mask = torch.zeros(len(x), dtype=torch.bool)
    val_mask = torch.zeros(len(x), dtype=torch.bool)
    test_mask = torch.zeros(len(x), dtype=torch.bool)

    train_mask[:len(x_train)] = 1
    val_mask[len(x_train):(len(x_train) + len(x_val))] = 1
    test_mask[(len(x_train) + len(x_val)):] = 1

    # Create a PyTorch Geometric Data object
    num_nodes = g.num_vertices()  # Number of nodes
    graph_edges = g.get_edges()  # Edge connections (from_idx, to_idx)
    edge_index = torch.tensor(graph_edges.T, dtype=torch.long)  # Edge connections (from_idx, to_idx)

    aact_graph_data = Data(x=x, edge_index=edge_index, y=y)

    # Assign masks to the Data object
    aact_graph_data.train_mask = train_mask
    aact_graph_data.val_mask = val_mask
    aact_graph_data.test_mask = test_mask
    aact_graph_data.edge_attr = torch.tensor(g.ep['e_weights'].a)  # Edge features (random for illustration)
    aact_graph_dataset = SingleObjectDataset(aact_graph_data)
    return aact_graph_dataset


# %%

class GNNModel(nn.Module):
    
    def __init__(self, c_in, c_hidden, c_out, num_layers=2, layer_name="GCN", dp_rate=0.1, **kwargs):
        """
        Inputs:
            c_in - Dimension of input features
            c_hidden - Dimension of hidden features
            c_out - Dimension of the output features. Usually number of classes in classification
            num_layers - Number of "hidden" graph layers
            layer_name - String of the graph layer to use
            dp_rate - Dropout rate to apply throughout the network
            kwargs - Additional arguments for the graph layer (e.g. number of heads for GAT)
        """
        super().__init__()
        gnn_layer = gnn_layer_by_name[layer_name]
        
        layers = []
        in_channels, out_channels = c_in, c_hidden
        for l_idx in range(num_layers-1):
            layers += [
                gnn_layer(in_channels=in_channels, 
                          out_channels=out_channels,
                          **kwargs),
                nn.ReLU(inplace=True),
                nn.Dropout(dp_rate)
            ]
            in_channels = c_hidden
        layers += [gnn_layer(in_channels=in_channels, 
                             out_channels=c_out,
                             **kwargs)]
        self.layers = nn.ModuleList(layers)
    
    def forward(self, x, edge_index):
        """
        Inputs:
            x - Input features per node
            edge_index - List of vertex index pairs representing the edges in the graph (PyTorch geometric notation)
        """
        for l in self.layers:
            # For graph layers, we need to add the "edge_index" tensor as additional input
            # All PyTorch Geometric graph layer inherit the class "MessagePassing", hence
            # we can simply check the class type.
            if isinstance(l, geom_nn.MessagePassing):
                x = l(x, edge_index)
            else:
                x = l(x)
        return x

class MLPModel(nn.Module):
    
    def __init__(self, c_in, c_hidden, c_out, num_layers=2, dp_rate=0.1):
        """
        Inputs:
            c_in - Dimension of input features
            c_hidden - Dimension of hidden features
            c_out - Dimension of the output features. Usually number of classes in classification
            num_layers - Number of hidden layers
            dp_rate - Dropout rate to apply throughout the network
        """
        super().__init__()
        layers = []
        in_channels, out_channels = c_in, c_hidden
        for l_idx in range(num_layers-1):
            layers += [
                nn.Linear(in_channels, out_channels),
                nn.ReLU(inplace=True),
                nn.Dropout(dp_rate)
            ]
            in_channels = c_hidden
        layers += [nn.Linear(in_channels, c_out)]
        self.layers = nn.Sequential(*layers)
    
    def forward(self, x, *args, **kwargs):
        """
        Inputs:
            x - Input features per node
        """
        return self.layers(x)

class NodeLevelGNN(pl.LightningModule):
    
    def __init__(self, model_name, **model_kwargs):
        super().__init__()
        # Saving hyperparameters
        self.save_hyperparameters()
        
        if model_name == "MLP":
            self.model = MLPModel(**model_kwargs)
        else:
            self.model = GNNModel(**model_kwargs)
        self.loss_module = nn.CrossEntropyLoss()

    def forward(self, data, mode="train"):
        x, edge_index = data.x, data.edge_index
        x = self.model(x, edge_index)
        
        # Only calculate the loss on the nodes corresponding to the mask
        if mode == "train":
            mask = data.train_mask
        elif mode == "val":
            mask = data.val_mask
        elif mode == "test":
            mask = data.test_mask
        else:
            assert False, f"Unknown forward mode: {mode}"
        
        loss = self.loss_module(x[mask], data.y[mask])
        acc = (x[mask].argmax(dim=-1) == data.y[mask]).sum().float() / mask.sum().float() 
        return loss, acc

    def configure_optimizers(self):
        # We use SGD here, but Adam works as well 
        optimizer = optim.SGD(self.parameters(), lr=0.1, momentum=0.9, weight_decay=2e-3)
        return optimizer

    def training_step(self, batch, batch_idx):
        loss, acc = self.forward(batch, mode="train")
        self.log('train_loss', loss)
        self.log('train_acc', acc)
        return loss

    def validation_step(self, batch, batch_idx):
        _, acc = self.forward(batch, mode="val")
        self.log('val_acc', acc)

    def test_step(self, batch, batch_idx):
        _, acc = self.forward(batch, mode="test")
        self.log('test_acc', acc)

def train_node_classifier(model_name, dataset, **model_kwargs):
    pl.seed_everything(42)
    node_data_loader = geom_data.DataLoader(dataset, batch_size=1)
    
    # Create a PyTorch Lightning trainer with the generation callback
    root_dir = os.path.join(CHECKPOINT_PATH, "NodeLevel" + model_name)
    os.makedirs(root_dir, exist_ok=True)
    trainer = pl.Trainer(default_root_dir=root_dir,
                        callbacks=[ModelCheckpoint(save_weights_only=True, mode="max", monitor="val_acc"), LearningRateMonitor(logging_interval="epoch")],
                         accelerator="gpu" if str(device).startswith("cuda") else "cpu",
                         devices=1,
                         max_epochs=200,
                         enable_progress_bar=True) # False because epoch size is 1
    trainer.logger._default_hp_metric = None # Optional logging argument that we don't need

    # Check whether pretrained model exists. If yes, load it and skip training
    pretrained_filename = os.path.join(CHECKPOINT_PATH, f"NodeLevel{model_name}.ckpt")
    if os.path.isfile(pretrained_filename):
        print("Found pretrained model, loading...")
        model = NodeLevelGNN.load_from_checkpoint(pretrained_filename)
    else:
        pl.seed_everything()
        model = NodeLevelGNN(model_name=model_name, c_in=dataset.x.shape[1], c_out=len(dataset.y.unique()), **model_kwargs)
        trainer.fit(model, node_data_loader, node_data_loader)
        model = NodeLevelGNN.load_from_checkpoint(trainer.checkpoint_callback.best_model_path)
    
    # Test best model on the test set
    test_result = trainer.test(model, node_data_loader, verbose=False)
    batch = next(iter(node_data_loader))
    batch = batch.to(model.device)
    _, train_acc = model.forward(batch, mode="train")
    _, val_acc = model.forward(batch, mode="val")
    result = {"train": train_acc,
              "val": val_acc,
              "test": test_result[0]['test_acc']}
    return model, result

# %%
# Small function for printing the test scores
def print_results(result_dict):
    if "train" in result_dict:
        print(f"Train accuracy: {(100.0*result_dict['train']):4.2f}%")
    if "val" in result_dict:
        print(f"Val accuracy:   {(100.0*result_dict['val']):4.2f}%")
    print(f"Test accuracy:  {(100.0*result_dict['test']):4.2f}%")