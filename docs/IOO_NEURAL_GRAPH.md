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
