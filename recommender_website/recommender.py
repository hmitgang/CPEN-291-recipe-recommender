import torch
import torchtext
import numpy as np
import pandas as pd
import csv
from torch import nn, optim
from tqdm.auto import tqdm
import matplotlib.pyplot as plt
import pdb


DEVICE = torch.device('cpu')
n_epochs = 1

ingr_map = pd.read_pickle("../datasets/our_ingr_map.pkl")
recipes = pd.read_pickle("../datasets/our_recipes.pkl")
interactions = pd.read_pickle(
    "../datasets/our_interactions.pkl")[['user_id', 'recipe_id', 'rating']]


class RecipeDataset(torch.utils.data.Dataset):
    def __init__(self, df):
        self.u2n = {u: n for n, u in enumerate(df['user_id'].unique())}
        self.r2n = {r: n for n, r in enumerate(df['recipe_id'].unique())}
        df['user_id_n'] = df['user_id'].apply(lambda u: self.u2n[u])
        df['recipe_id_n'] = df['recipe_id'].apply(lambda r: self.r2n[r])
        self.df = df
        self.coords = torch.LongTensor(df[['user_id_n', 'recipe_id_n']].values)
        self.ratings = torch.FloatTensor(df['rating'].values)
        self.n_users = df['user_id_n'].nunique()
        self.n_recipes = df['recipe_id_n'].nunique()

    def __len__(self):
        return len(self.coords)

    def __getitem__(self, i):
        return (self.coords[i], self.ratings[i])


class RecipeRecs(nn.Module):
    def __init__(self, n_users, n_recipes, emb_dim):
        super(RecipeRecs, self).__init__()
        self.user_emb = nn.Embedding(n_users, emb_dim)
        self.user_bias = nn.Embedding(n_users, 1)
        self.recipe_emb = nn.Embedding(n_recipes, emb_dim)
        self.recipe_bias = nn.Embedding(n_recipes, 1)
        nn.init.xavier_uniform_(self.user_emb.weight)
        nn.init.xavier_uniform_(self.recipe_emb.weight)
        nn.init.zeros_(self.user_bias.weight)
        nn.init.zeros_(self.recipe_bias.weight)

    def forward(self, samples):
        users = self.user_emb(samples[:, 0])
        recipes = self.recipe_emb(samples[:, 1])
        dot = (users * recipes).sum(1)
        user_b = self.user_bias(samples[:, 0]).squeeze()
        recipe_b = self.recipe_bias(samples[:, 1]).squeeze()
        return dot + user_b + recipe_b


def run_test(model, ldr, crit):
    total_loss, total_count = 0, 0
    model.eval()
    tq_iters = tqdm(ldr, leave=False, desc='test iter')
    with torch.no_grad():
        for coords, labels in tq_iters:
            coords, labels = coords.to(DEVICE), labels.to(DEVICE)
            preds = model(coords)
            loss = crit(preds, labels)
            total_loss += loss.item() * labels.size(0)
            total_count += labels.size(0)
            tq_iters.set_postfix(
                {'loss': total_loss/total_count}, refresh=True)
    return total_loss / total_count


def run_train(model, ldr, crit, opt, sched, epoch, progress_func):
    model.train()
    total_loss, total_count = 0, 0
    tq_iters = tqdm(ldr, leave=False, desc='train iter')
    for (i, (coords, labels)) in enumerate(tq_iters):
        opt.zero_grad()
        coords, labels = coords.to(DEVICE), labels.to(DEVICE)
        preds = model(coords)
        loss = crit(preds, labels)
        loss.backward()
        opt.step()
        sched.step()
        total_loss += loss.item() * labels.size(0)
        total_count += labels.size(0)
        tq_iters.set_postfix({'loss': total_loss/total_count}, refresh=True)
        if progress_func and i % 300 == 0:
            progress_func(f"{100*(epoch/n_epochs + i/len(ldr)):.2f}%")
    return total_loss / total_count


def run_all(model, ldr_train, ldr_test, crit, opt, sched, n_epochs=10):
    best_loss = np.inf
    tq_epochs = tqdm(range(n_epochs), desc='epochs', unit='ep')
    for epoch in tq_epochs:
        train_loss = run_train(model, ldr_train, crit, opt, sched)
        test_loss = run_test(model, ldr_test, crit)
        tqdm.write(
            f'epoch {epoch}   train loss {train_loss:.6f}    test loss {test_loss:.6f}')
        if test_loss < best_loss:
            best_loss = test_loss
            tq_epochs.set_postfix({'bE': epoch, 'bL': best_loss}, refresh=True)


def get_recipe_by_id(id, attr="name"):
    x = recipes[recipes["recipe_id"] == id][attr]
    if len(x) < 1:
        return "Unknown"
    else:
        return x.values[0]


def get_recommendations_for_user(model, dataset, user_id, ingr, exclude_ingr, batch_size=32):
    ingr = ingr_name_to_ids(ingr)
    exclude_ingr = ingr_name_to_ids(exclude_ingr)
    allowed_recipes = set(recipes[recipes["ingredient_ids"].apply(
        lambda ids: not ingr.isdisjoint(ids) and exclude_ingr.isdisjoint(ids)
    )]["recipe_id"].unique()).intersection(set(dataset.r2n.keys()))
    user_n = dataset.u2n[user_id]
    ratings = []
    n2r = {value: key for key, value in dataset.r2n.items()}
    model.eval()
    with torch.no_grad():
        for coords in torch.LongTensor([[user_n, dataset.r2n[r]] for r in allowed_recipes]).split(batch_size):
            coords = coords.to(DEVICE)
            preds = model(coords)
            ratings += [(n2r[int(coords[i, 1])], float(preds[i]))
                        for i in range(preds.shape[0])]
    return sorted(ratings, key=lambda x: x[1], reverse=True)


def ingr_name_to_ids(ingr):
    return set(ingr_map[ingr_map["replaced"].isin(ingr)]["id"].unique())


def recommend(new_data, ingr, progress_func=None, user_id=-1):
    ingr = set(ingr)
    replacements = {
        "bellpeppers": "bell pepper",
        "greenbeans": "green bean",
        "olives": "olive",
        "onions": "onion",
        "potatoes": "potato",

    }
    exclude_ingr = {
        "apple", "avocado", "banana", "beef", "bellpeppers", "bread",
        "broccoli", "cabbage", "cheese", "chicken", "corn", "cucumber", "egg",
        "eggplant", "greenbeans", "lemon", "lettuce", "mushroom", "olives",
        "onions", "pasta", "potatoes", "rice", "salmon", "spinach", "tomato"
    } - ingr
    ingr = set(
        map(lambda x: replacements[x] if x in replacements else x, ingr))
    exclude_ingr = set(
        map(lambda x: replacements[x] if x in replacements else x, exclude_ingr))

    new_recipe_ids, new_ratings = zip(*new_data.items())
    new_interactions = pd.concat([interactions, pd.DataFrame(
        {
            "user_id": -1,
            "recipe_id": new_recipe_ids,
            "rating": [int(_) for _ in new_ratings],
        }
    )])
    ds_full = RecipeDataset(new_interactions)
    model = RecipeRecs(ds_full.n_users, ds_full.n_recipes, 20)
    model.to(DEVICE)

    ldr_train = torch.utils.data.DataLoader(
        ds_full, batch_size=32, shuffle=True)

    crit = nn.MSELoss().to(DEVICE)
    opt = optim.SGD(model.parameters(), lr=1e-6, momentum=0.9)
    sched = optim.lr_scheduler.OneCycleLR(
        opt, max_lr=0.4, steps_per_epoch=len(ldr_train), epochs=n_epochs)

    for i in range(n_epochs):
        train_loss = run_train(model, ldr_train, crit,
                               opt, sched, i, progress_func)
        print(f"epoch { i } -- train_loss: { train_loss }")

    if progress_func:
        progress_func(f"{100:.2f}%")
    return get_recommendations_for_user(model, ds_full, user_id, ingr, exclude_ingr)


#
# ds_full = RecipeDataset(interactions)

# model = RecipeRecs(ds_full.n_users, ds_full.n_recipes, 20)
# model.to(DEVICE)

# ldr_train = torch.utils.data.DataLoader(ds_train, batch_size=32, shuffle=True)
# ldr_test = torch.utils.data.DataLoader(ds_test, batch_size=32)

# n_epochs = 5

# crit = nn.MSELoss().to(DEVICE)
# opt = optim.SGD(model.parameters(), lr=1e-6, momentum=0.9)
# sched = optim.lr_scheduler.OneCycleLR(
#     opt, max_lr=0.4, steps_per_epoch=len(ldr_train), epochs=n_epochs)

# run_all(model, ldr_train, ldr_test, crit, opt, sched, n_epochs)


#


#
# get_recommendations_for_user(model, ds_full, 2046)[:10]
