import json
import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from predict import estimate

NUM_TRUCKS = 3
TRUCK_CAPACITY_L = 8000
RESERVE = 0.10           # 10% din capacitate ramane libera pentru alerte
MANDATORY_FILL = 0.8      # peste acest nivel, pubela trebuie golita azi
HORIZON_H = 24            # urmatoarea colectare e peste 24 de ore
PENALTY_SCALE = 4000

with open("bins.json") as f:
    data = json.load(f)
with open("history.json") as f:
    history = json.load(f)
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
    demand_idx, 0, [int(TRUCK_CAPACITY_L * (1 - RESERVE))] * NUM_TRUCKS, True, "Capacity")

predictions, mandatory_by_forecast = {}, 0
for node, b in enumerate(bins, start=1):
    rate, hours_to_full = estimate(history[b["id"]])
    if rate is None:                         # prea putine date: folosim nivelul curent
        predicted = b["fill_level"]
    else:
        predicted = min(1.0, b["fill_level"] + rate * HORIZON_H)
    predictions[b["id"]] = {"predicted_fill_24h": round(predicted, 2),
                            "hours_to_full": None if hours_to_full is None else round(hours_to_full, 1)}

    will_overflow = hours_to_full is not None and hours_to_full <= HORIZON_H
    if b["fill_level"] >= MANDATORY_FILL or will_overflow:
        if b["fill_level"] < MANDATORY_FILL:
            mandatory_by_forecast += 1
        continue                             # obligatorie: fara disjunctie
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
# verificare: cate pubele sarite ar da pe dinafara pana la urmatoarea colectare (adevarul simulat)
overflow_skipped = sum(
    1 for i in range(1, n) if i not in visited
    and bins[i - 1]["fill_level"] + bins[i - 1]["fill_rate_per_h"] * HORIZON_H >= 1.0)

with open("baseline_routes.json") as f:
    baseline_m = json.load(f)["total_distance_m"]

print(f"Total: {total_m / 1000:.1f} km (baseline {baseline_m / 1000:.1f} km, "
      f"economie {100 * (1 - total_m / baseline_m):.0f}%)")
print(f"Obligatorii din cauza predictiei: {mandatory_by_forecast}")
print(f"Pubele sarite: {len(skipped)}, dintre care ar da pe dinafara in {HORIZON_H}h: {overflow_skipped}")

with open("smart_routes.json", "w") as f:
    json.dump({"total_distance_m": total_m, "routes": routes, "skipped": skipped,
               "predictions": predictions}, f, indent=2)
