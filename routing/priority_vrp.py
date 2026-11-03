import json
import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

NUM_TRUCKS = 3
TRUCK_CAPACITY_L = 8000
MANDATORY_FILL = 0.8      # peste acest nivel, pubela trebuie golita azi
PENALTY_SCALE = 4000      # cati metri "valoreaza" o pubela plina 100%

with open("bins.json") as f:
    data = json.load(f)
bins = data["bins"]
M = np.load("distance_matrix.npy")
n = len(M)

demands = [0] + [round(b["fill_level"] * b["capacity_l"]) for b in bins]

manager = pywrapcp.RoutingIndexManager(n, NUM_TRUCKS, 0)
routing = pywrapcp.RoutingModel(manager)

def distance_cb(from_index, to_index):
    return int(M[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)])

transit = routing.RegisterTransitCallback(distance_cb)
routing.SetArcCostEvaluatorOfAllVehicles(transit)

def demand_cb(from_index):
    return demands[manager.IndexToNode(from_index)]

demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
routing.AddDimensionWithVehicleCapacity(
    demand_idx, 0, [TRUCK_CAPACITY_L] * NUM_TRUCKS, True, "Capacity")

# pubelele sub prag devin optionale: pot fi sarite, dar cu o penalitate
for node, b in enumerate(bins, start=1):
    if b["fill_level"] < MANDATORY_FILL:
        penalty = int(PENALTY_SCALE * b["fill_level"] ** 2)
        routing.AddDisjunction([manager.NodeToIndex(node)], penalty)

params = pywrapcp.DefaultRoutingSearchParameters()
params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
params.time_limit.FromSeconds(10)

solution = routing.SolveWithParameters(params)
if solution is None:
    raise SystemExit("Nu s-a gasit nicio solutie: verifica capacitatea camioanelor.")

routes, total_m, visited = [], 0, set()
for v in range(NUM_TRUCKS):
    idx = routing.Start(v)
    stops, dist, load = [], 0, 0
    while not routing.IsEnd(idx):
        node = manager.IndexToNode(idx)
        stops.append(node)
        visited.add(node)
        load += demands[node]
        nxt = solution.Value(routing.NextVar(idx))
        dist += routing.GetArcCostForVehicle(idx, nxt, v)
        idx = nxt
    stops.append(0)
    total_m += dist
    routes.append({"truck": v, "stops": stops, "distance_m": dist, "load_l": load})
    print(f"Camion {v}: {len(stops) - 2} pubele, {dist / 1000:.1f} km, {load} L")

skipped = [bins[i - 1]["id"] for i in range(1, n) if i not in visited]
skipped_fill = [bins[i - 1]["fill_level"] for i in range(1, n) if i not in visited]

with open("baseline_routes.json") as f:
    baseline_m = json.load(f)["total_distance_m"]

print(f"Total: {total_m / 1000:.1f} km (baseline {baseline_m / 1000:.1f} km, "
      f"economie {100 * (1 - total_m / baseline_m):.0f}%)")
print(f"Pubele sarite: {len(skipped)}, nivel maxim printre ele: "
      f"{max(skipped_fill, default=0):.2f}")

with open("priority_routes.json", "w") as f:
    json.dump({"total_distance_m": total_m, "routes": routes, "skipped": skipped}, f, indent=2)
