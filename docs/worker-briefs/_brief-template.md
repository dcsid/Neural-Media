# Worker brief — TEMPLATE

(Not a real brief. Reference for the integration lead when writing new ones.)

## Mission

One sentence. What this worker delivers, in plain language.

## Owned files / directories

Exact paths this worker is responsible for. Workers may freely add files
inside these paths; everything else is read-only to them.

## Files this worker must NOT touch

The list of cross-cutting files: `shared/`, peer workers' directories, root
configs. Modification requires a coordinated PR via the integration lead.

## Deliverables

A short list of concrete artifacts the worker produces, with acceptance
criteria.

## Interfaces this worker must preserve

The exact function signatures, route shapes, or file formats other workers
depend on. If a worker needs to change one, they coordinate via the
integration lead.

## How to test the work

Specific commands. Each brief should be testable in isolation without the
other workers' branches being ready.

## Scientific-framing constraints

Carry the constraints from `docs/scientific-framing.md` that apply to this
worker's surface (copy, charts, alerts, claims).

## Out of scope for this worker

What this worker should resist doing, even if tempted.
