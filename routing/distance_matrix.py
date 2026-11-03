import json
import numpy as np
import networkx as nx
import osmnx as ox

G = ox.load_graphml("timisoara_drive.graphml")
with open("bins.json") as f:
    data = json.load(f)

# index 0 = depozit, 1..60 = pubele
nodes = [data["depot"]["node"]] + [b["node"] for b in data["bins"]]
n = len(nodes)

M = np.zeros((n, n), dtype=int)
for i, src in enumerate(nodes):
    lengths = nx.single_source_dijkstra_path_length(G, src, weight="length")
    for j, dst in enumerate(nodes):
        M[i, j] = round(lengths[dst])

np.save("distance_matrix.npy", M)

asym = np.sum(M != M.T) // 2
print(f"Matrice {n}x{n} salvata in distance_matrix.npy")
print(f"Distanta medie: {M[M > 0].mean():.0f} m, maxima: {M.max()} m")
print(f"Perechi asimetrice (sensuri unice): {asym}")