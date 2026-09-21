STATETREE CORE INVARIANTS
=========================

1. IMMUTABLE COMMITS

Once a StateTree commit is created,
its contents can never change.

New information creates a new commit.


2. VERIFIED CANONICAL STATE

Only verified state may advance HEAD.

Unverified work stays inside a branch.


3. BRANCH ISOLATION

Failure in branch B must not contaminate
the active context or state of branch A.


4. ALIGNED RECOVERY

A recoverable checkpoint must represent:

Agent state
+
Execution/environment state

Restoring only the conversation is not
considered a successful rollback.


5. BOUNDED ACTIVE CONTEXT

The model does not receive the complete
execution history.

Active context is constructed from:

goal
+
current root-to-HEAD path
+
active subgoal
+
verified state
+
recent trace
+
selectively retrieved branch knowledge


6. PROVENANCE

Every derived fact should be traceable
to the state/action/evidence that produced it.

If a fact is invalidated, dependent state can
also be identified and invalidated.


7. DETERMINISTIC COMPACTION FIRST

Before using an LLM to summarize memory:

remove duplicates
remove old tool output
remove redundant logs
remove superseded state
remove deterministic noise

LLM summarization occurs only afterward.


8. SELECTIVE MEMORY INJECTION

Stored memory is not automatically placed
into every prompt.

StateTree decides whether information is
decision-relevant before injecting it.


9. SAFE MIGRATION

Changing models/workers follows:

QUIESCE
→ CHECKPOINT
→ VALIDATE
→ BIND
→ REHYDRATE
→ RESUME


10. CONCURRENCY SAFETY

Two branches cannot silently overwrite
the same canonical state.

Every write specifies which parent commit
it was derived from.

HEAD movement is atomic.