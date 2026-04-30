# IOO Neural Graph + Screen Graph

## Core model

The IOO Graph is not a flat content taxonomy. It is a neural graph — a living neural network inside Ora's brain and nervous system.

Each IOO node represents a possible activity, experience, sub-goal, goal, capability, or pathway in the user's life. Edges represent learned transitions: what tends to lead somewhere meaningful, what requires preparation, what clarifies intent, and what can be executed now.

Ora should treat the graph as an embodied map of possibility, not as a menu of static pages.

## Screen Graph

Generated screens are durable pathway/interface nodes. A screen is not merely a UI page; it is an intermediary pathway node between:

1. the user's current state, capabilities, constraints, preferences, and vector;
2. the IOO possibilities available in Ora's graph;
3. the execution layer that turns an option into action.

A generated screen should be stored with its own embedding and graph relationships. The same underlying IOO node may produce many different screen nodes depending on user context, timing, domain, and presentation strategy.

## Screen Pattern Library

Screen patterns are reusable interface/pathway templates that Ora can adapt into generated screen nodes. A pattern is not the final screen; it is the learned shape of an interaction that tends to help a certain user/context move forward.

Patterns should support variants so Ora can test different presentations without losing the underlying intent. The lightweight lifecycle is:

1. **Create/reuse** — create a new pattern only when no existing pattern fits; otherwise reuse and adapt the closest successful pattern.
2. **Test variants** — generate small variants for layout, copy, sequencing, or interaction mechanism.
3. **Reinforce winners** — increase usage/weight for variants that produce completion, clarity, execution, or other positive outcomes.
4. **Trim stale/unused/low-outcome patterns** — deprecate or prune patterns and variants that are not used, have not been used recently, or consistently underperform.

Useful pruning fields include `last_used_at`, `usage_count`, `success_score` / `outcome_score`, `deprecated_at`, and `pruned_at`. Pruning should be conservative: prefer marking stale assets as deprecated/pruned before permanent deletion so Ora can preserve learning history.

Important screen relationships:

- `leads_to` — this screen naturally routes toward another screen or outcome.
- `requires` — this screen depends on another condition, preparation, or prerequisite screen.
- `clarifies` — this screen helps Ora gather user intent or missing state.
- `executes` — this screen initiates a concrete action or IOO execution run.
- `belongs_to_ioo_node` — this screen is an interface/pathway for a specific IOO node.

## Option spindles: A → B

When a user wants to move from point A to point B, Ora should not construct a single brittle path. It should construct multiple organic option-spindles: bundles of possible routes, each with different tradeoffs.

Example pathway dimensions:

- fastest route;
- easiest route;
- most fulfilling route;
- lowest-cost route;
- most growth-oriented route;
- most social route;
- most aligned with the user's current nervous system state.

Each spindle can contain IOO nodes, generated screen nodes, clarifying screens, and execution steps. User behaviour continuously updates the ranking and weights of these routes.

## User state → screen path → execution

The intended loop is:

1. Ora reads the user state/vector: goals, mood, capabilities, constraints, history, preferences.
2. Ora proposes IOO possibilities and generates one or more screen pathway nodes.
3. The user responds through explicit or implicit signals: Do Now, Do Later, Not Interested, dwell, skip, save, rate, execute.
4. Ora updates both:
   - the user vector; and
   - the Screen → IOO → Execution graph relationship.
5. Ora refines future option spindles from the new state.

The screen is therefore both an interface and a learning event.

## Domains as modules, not fixed destinations

Apps/domains such as iVive, Eviva, and Aventi are domains of activity and reusable modules. They bend around the user's path.

- iVive can contribute vitality actions to a mission pathway.
- Eviva can turn contribution into meaning, reputation, and service.
- Aventi can turn exploration into lived experience and discovery.

The user's path chooses and recombines modules. The modules should not trap the user in rigid silos.

## Ora's brain and nervous system

The IOO Graph is part of Ora's brain: it models meaning, capability, possibility, and progression.

The Screen Graph is part of Ora's nervous system: it senses which interfaces move a user toward fulfilment, which confuse them, which invite execution, and which should be suppressed.

Together they form a feedback loop:

```text
user state/vector
  → IOO possibility graph
  → generated screen graph
  → user action / feedback / execution
  → updated user state + graph weights
  → better option spindles
```

This is the foundation for Ora as an adaptive AI operating system for human flourishing.

## Living graph lifecycle

The production model now treats IOO as a living neural graph rather than a static library of cards.

Each node carries lifecycle metadata:

- `neural_state` — `active`, `pruned`, `merged`, etc.
- `generation_source` — seed, neural growth, neural split, imported source, agent proposal.
- `growth_angle` — fastest, easiest, most fulfilling, social, low-cost, growth edge, novelty, vitality, contribution.
- `parent_node_ids`, `split_from_node_id`, `merged_into_node_id`, `merged_from_node_ids` — lineage.
- `engagement_score` and `fulfilment_score` — behavioural reinforcement targets.
- `spawned_count`, `last_reinforced_at`, `pruned_at`, `prune_reason` — lifecycle control.

Edges also carry richer neural meaning:

- `relation_type` — `leads_to`, `requires`, `branches_to`, `splits_to`, `merged_path`, etc.
- `confidence`, `rationale`, `last_reinforced_at`, `pruned_at`.

Every graph mutation can be written to `ioo_graph_events`, giving Ora a memory of how the graph evolved.

## Growth from multiple angles

When a user intention or IOO node is promising, Ora should not generate one path. It should grow a small family of branches from different angles:

- fastest route;
- easiest route;
- most fulfilling route;
- most social route;
- lowest-cost route;
- growth-edge route;
- vitality-first route;
- contribution/service route;
- novelty/adventure route.

Endpoint:

```http
POST /api/ioo/nodes/{node_id}/grow
```

Example body:

```json
{
  "angles": ["fastest", "most_fulfilling", "most_social", "lowest_cost"],
  "max_new": 4
}
```

This creates child/sibling nodes connected by `branches_to` edges. The graph can then learn which angle actually creates engagement, activity, completed experiences, and fulfilment.

## Reinforce, prune, split, merge

User behaviour updates the graph:

- surface `view` lightly reinforces engagement;
- surface `interact` and execution `start` reinforce stronger intent;
- execution/surface `complete` reinforces engagement and fulfilment;
- `goal_success` strongly reinforces fulfilment;
- abandoned/low-signal paths decay over time.

Lifecycle sweeps can then:

1. recalculate edge weights from traversal/success data;
2. prune weak branches conservatively while preserving history;
3. merge exact duplicate nodes into the strongest survivor;
4. grow promising high-engagement nodes into new angled branches;
5. split overloaded broad nodes into sharper focus nodes when needed.

Endpoint:

```http
POST /api/ioo/neural/lifecycle/sweep
```

The sweep follows the principle:

```text
grow from multiple angles → reinforce winners → prune weak branches → split broad successes → merge duplicates → repeat
```

## Engagement is not the final objective

The graph optimises for engagement only as a nervous-system signal. The higher objective remains more lived experiences, meaningful activity, vitality, contribution, and fulfilment. A route that gets clicks but does not produce execution or fulfilment should eventually lose weight to routes that move someone into real life.
