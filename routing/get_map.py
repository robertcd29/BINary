import osmnx as ox

# Centrul si raza acopera toate ghenele din tabela trashes a lui Robert
# (cea mai departata, TM-003 pe Ovidiu Cotrus, e la ~5 km de centru).
CENTER = (45.750, 21.232)   # Complex Studențesc, Timișoara
G = ox.graph_from_point(CENTER, dist=6500, network_type="drive")
G = ox.truncate.largest_component(G, strongly=True)

print(f"Noduri: {len(G.nodes)}, Muchii: {len(G.edges)}")
ox.save_graphml(G, "timisoara_drive.graphml")
print("Harta salvata in timisoara_drive.graphml")
