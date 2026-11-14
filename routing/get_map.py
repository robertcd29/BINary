import osmnx as ox

# Dreptunghiul acopera toate ghenele din tabela trashes a lui Robert (TM-001 ... TM-017),
# cu ~1 km margine, ca drumurile dintre ele sa fie complete.
BBOX = (21.185, 45.700, 21.280, 45.790)   # vest, sud, est, nord
G = ox.graph_from_bbox(BBOX, network_type="drive")
G = ox.truncate.largest_component(G, strongly=True)

print(f"Noduri: {len(G.nodes)}, Muchii: {len(G.edges)}")
ox.save_graphml(G, "timisoara_drive.graphml")
print("Harta salvata in timisoara_drive.graphml")
