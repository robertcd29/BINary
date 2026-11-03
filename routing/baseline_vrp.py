import json
import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

NUM_TRUCKS = 3
TRUCK_CAPACITY_L = 8000   # volum dupa compactare, valoare de simulare

with open("bins.json") as f:
    data = json.load(f)
bins = data["bins"]
M = np.load("distance_matrix.npy")
n = len(M)

# cantitatea de colectat din fiecare punct (depozitul = 0)
demands = [0] + [round(b["fill_level"] * b["capacity_l"]) for b in bins]

manager = pywrapcp.RoutingIndexManager(n, NUM_TRUCKS, 0)
routing = pywrapcp.RoutingModel(manager)

def distance_cb(from_index, to_index):
    i = manager.IndexToNode(from_index)
    j = manager.IndexToNode(to_index)
    return int(M[i][j])

transit = routing.RegisterTransitCallback(distance_cb)
routing.SetArcCostEvaluatorOfAllVehicles(transit)

def demand_cb(from_index):
    return demands[manager.IndexToNode(from_index)]

demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
routing.AddDimensionWithVehicleCapacity(
    demand_idx, 0, [TRUCK_CAPACITY_L] * NUM_TRUCKS, True, "Capacity")

params = pywrapcp.DefaultRoutingSearchParameters()
params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
params.time_limit.FromSeconds(30)

solution = routing.SolveWithParameters(params)
if solution is None:
    raise SystemExit("Nu s-a gasit nicio solutie: verifica capacitatea camioanelor.")

routes, total_m = [], 0
for v in range(NUM_TRUCKS):
    idx = routing.Start(v)
    stops, dist, load = [], 0, 0
    while not routing.IsEnd(idx):
        node = manager.IndexToNode(idx)
        stops.append(node)
        load += demands[node]
        nxt = solution.Value(routing.NextVar(idx))
        dist += routing.GetArcCostForVehicle(idx, nxt, v)
        idx = nxt
    stops.append(0)
    total_m += dist
    routes.append({"truck": v, "stops": stops, "distance_m": dist, "load_l": load})
    print(f"Camion {v}: {len(stops) - 2} pubele, {dist / 1000:.1f} km, {load} L")

print(f"Total: {total_m / 1000:.1f} km")
with open("baseline_routes.json", "w") as f:
    json.dump({"total_distance_m": total_m, "routes": routes}, f, indent=2)
