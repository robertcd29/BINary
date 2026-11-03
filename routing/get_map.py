import osmnx as ox

CENTER = (45.750, 21.232)   # Complex Studențesc, Timișoara
G = ox.graph_from_point(CENTER, dist=2500, network_type="drive")
G = ox.truncate.largest_component(G, strongly=True)

print(f"Noduri: {len(G.nodes)}, Muchii: {len(G.edges)}")
ox.save_graphml(G, "timisoara_drive.graphml")
ox.plot_graph(G, node_size=5)