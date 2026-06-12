# EXPANDED KNOWLEDGE

This database contains highly compressed, local-first engineering and mathematical patterns.

Each pattern is encapsulated within a structured XML schema:

<pattern domain="[domain]" name="[pattern name]">
  <why>[performance/architectural justification]</why>
  <how>[concrete implementation code or execution steps]</how>
  <pitfall>[critical failure mode or edge case to prevent]</pitfall>
</pattern>

INSTRUCTION: When these patterns are retrieved and injected into your active context [INGESTED EVIDENCE], you must strictly implement the logic specified inside the <how> tags and actively defend against the failures detailed inside the <pitfall> tags.

---

<pattern domain="reasoning" name="Knowledge Base Prioritization">
  <why>Forces the agent to prioritize the local knowledge base over generic internet patterns.</why>
  <how>When performing PLAN or DEEP coding tasks, retrieve patterns from the Expanded Knowledge database first. If a retrieved pattern is applicable, you MUST use its implementation strategy and enforce its pitfall guardrails as primary logic.</how>
  <pitfall>Blindly applying a retrieved pattern to a scenario where the prerequisites do not hold.</pitfall>
</pattern>

---

<pattern domain="systems" name="Epoch-Bounded Reclamation">
  <why>Epoch fencing turns unsafe object reuse into a deterministic quiescence problem and eliminates heisenbugs in lock-free teardown.</why>
  <how>Publish a global epoch on entry, retire nodes into per-thread limbo bags tagged with that epoch, and reclaim only after min(active epochs) exceeds retire epoch plus one. Keep the bags cacheline-aligned, batch scans, and use acquire-release only on the epoch snapshot; avoid full fences on the hot path.</how>
  <pitfall>Any thread that stalls while holding an old epoch can indefinitely delay reclamation and silently inflate memory.</pitfall>
</pattern>

---

<pattern domain="database" name="Write-Ahead Split Marker">
  <why>A split marker makes B-tree node splits idempotent across crashes by separating intent from materialization.</why>
  <how>Append a WAL record containing parent page id, child boundary key, and new sibling page ids before mutating pages. Flush WAL, then install a transient split marker in the parent, copy keys, and finally replace the marker with the real fence key after both pages are durable.</how>
  <pitfall>If the marker is not replayed as a first-class state, recovery can duplicate or orphan a subtree.</pitfall>
</pattern>

---

<pattern domain="security" name="Constant-Time Reject Envelope">
  <why>Equalizing reject paths removes timing oracles that otherwise leak which validation gate failed.</why>
  <how>Accumulate all validation failures into a single bitmask, defer branching until the end, and run a fixed-cost dummy MAC or compare on every request. Normalize parser work, pad variable-length inputs to a ceiling, and return a single generic failure code after a constant budget.</how>
  <pitfall>Early exits in helper functions or allocations can reintroduce measurable timing skew even when the top-level branch is flat.</pitfall>
</pattern>

---

<pattern domain="ml" name="Logit Lens Sanity Probe">
  <why>Interrogating intermediate residual stream logits exposes representation drift before it becomes user-visible hallucination.</why>
  <how>Project each layer's residual stream through the unembedding matrix, compare top-k token stability across depth, and track entropy collapse relative to a reference prompt set. Alert when an early layer already commits to a rare token class or when later layers invert a previously sharp distribution.</how>
  <pitfall>Top-token agreement can look healthy while probability mass shifts catastrophically among semantically similar alternatives.</pitfall>
</pattern>

---

<pattern domain="reasoning" name="Adversarial Counterexample First">
  <why>Searching for the smallest counterexample before proof prevents elegant but false invariants from surviving review.</why>
  <how>State the claim in quantifier form, then attempt to violate it with the lowest-dimensional case, degenerate boundary, and one-step perturbation. If none fail, generalize only after proving the counterexample space is closed under the transformation you care about.</how>
  <pitfall>Overfitting to a single counterexample can hide the true failure surface in a larger parameter regime.</pitfall>
</pattern>

---

<pattern domain="coding" name="Two-Phase Parse and Commit">
  <why>Separating syntax recognition from state mutation prevents partial ASTs from contaminating runtime state.</why>
  <how>First build a pure intermediate tree with source spans and unresolved symbols; second, validate invariants and commit by rewriting references into canonical handles. Keep construction side-effect free, freeze the tree before resolution, and make the commit phase the only place allowed to allocate runtime objects.</how>
  <pitfall>Hidden mutations during parse callbacks make rollback incomplete and create phantom symbols.</pitfall>
</pattern>

---

<pattern domain="infrastructure" name="Dual-Stack Canary Drain">
  <why>Draining one network path at a time preserves availability while exposing asymmetric failure in multi-path deployments.</why>
  <how>Route a fixed percentile of traffic through the new stack, pin canaries by session hash, and compare tail latency plus error class distributions against the old path. Only promote after both stacks observe identical retry behavior and DNS or service discovery TTLs have fully expired.</how>
  <pitfall>Shared dependencies can make both paths fail identically, masking regressions until full cutover.</pitfall>
</pattern>

---

<pattern domain="state" name="Monotonic State Stamp">
  <why>A monotonically increasing stamp gives every transition an ordering that survives retries, crashes, and duplication.</why>
  <how>Attach a strictly increasing stamp to each state mutation, persist the stamp before side effects, and reject any incoming transition whose stamp is not greater than the local watermark. Use the stamp for deduplication, replay protection, and causal debugging across replicas.</how>
  <pitfall>Clock-based stamps are not monotonic under skew, leap corrections, or restored snapshots.</pitfall>
</pattern>

---

<pattern domain="math" name="Scaled Residual Norm Budget">
  <why>Normalizing updates by residual norm keeps iterative solvers stable when magnitude varies by orders of magnitude.</why>
  <how>At each iteration compute r = b minus A x, then scale the step by min(1, tau / norm(r)). Use a fallback damping factor when norm(r) is near machine epsilon, and maintain a separate convergence test on both residual and parameter delta to avoid false convergence.</how>
  <pitfall>Pure residual stopping can stall on ill-conditioned systems where the iterate moves but the residual barely changes.</pitfall>
</pattern>

---

<pattern domain="security" name="Capability Sealing Token">
  <why>Sealing authority into an unforgeable token prevents confused-deputy escalation through ambient privileges.</why>
  <how>Bind token claims to audience, expiration, nonce, and an invocation scope hash, then sign with a key whose verification path is isolated from application logic. Validate audience before deserialization, enforce one-time nonce reuse checks, and derive per-resource sub-capabilities instead of passing raw bearer power.</how>
  <pitfall>A valid signature without strict audience and scope binding still allows replay in the wrong context.</pitfall>
</pattern>

---

<pattern domain="database" name="Covering Read Horizon">
  <why>Serving reads from a versioned horizon reduces index lookups and makes snapshot semantics explicit.</why>
  <how>Materialize a read-horizon watermark at transaction start, route all secondary index probes through pages whose commit version is less than or equal to that watermark, and retain only versions needed by active horizons. Compact with horizon-aware garbage collection so old versions disappear only after the slowest reader passes them.</how>
  <pitfall>Ignoring long-lived readers causes premature version pruning and silent snapshot corruption.</pitfall>
</pattern>

---

<pattern domain="systems" name="Cacheline Ownership Handoff">
  <why>Explicitly transferring ownership of a hot cacheline avoids ping-pong traffic and makes contention visible.</why>
  <how>Assign one writer per cacheline shard, aggregate mutations locally, and publish ownership transfers through a single atomic flag rather than writing the payload from multiple cores. Pad metadata away from data, keep the transfer path branchless, and batch handoffs at fixed thresholds to amortize coherence cost.</how>
  <pitfall>False sharing in adjacent metadata can erase the benefit even when the payload is perfectly partitioned.</pitfall>
</pattern>

---

<pattern domain="ml" name="Temperature Annealed Retrieval">
  <why>Annealing retrieval temperature over the decoding horizon favors precision early and diversity later.</why>
  <how>Set a low temperature for the first tokens to lock task intent, then raise it as entropy demand increases or as evidence coverage saturates. Couple the schedule to retrieval confidence so high-confidence citations stay deterministic while speculative completion regions become slightly broader.</how>
  <pitfall>Raising temperature too soon can destabilize chain-of-thought and produce inconsistent facts.</pitfall>
</pattern>

---

<pattern domain="reasoning" name="Invariant-Driven Decomposition">
  <why>Carving a problem around preserved quantities turns an open-ended search into a proof obligation.</why>
  <how>Identify one conserved measure, one decreasing measure, and one bounded measure; reframe every subproblem in terms of how it preserves or changes them. Reject decompositions that do not make at least one measure explicit, then solve each subproblem by proving the local update respects the global invariant.</how>
  <pitfall>Invented invariants that are not actually preserved create a false sense of completeness.</pitfall>
</pattern>

---

<pattern domain="coding" name="Sentinel-Guarded Iterator Advance">
  <why>A sentinel node removes edge branches and makes traversal logic uniform under mutation.</why>
  <how>Represent list ends with a permanent sentinel whose next and prev point to itself, and advance iterators by comparing identity to the sentinel rather than null. On deletion, relink neighbors before invalidating payload, and never expose raw end pointers to callers.</how>
  <pitfall>Forgetting to treat the sentinel as immortal can turn a clean invariant into a use-after-free.</pitfall>
</pattern>

---

<pattern domain="state" name="Branchless Conflict Bitmap">
  <why>Encoding concurrent conflicts in a bitmap lets arbitration stay deterministic and cheap.</why>
  <how>Assign each resource class a bit position, OR incoming claims into a word, and resolve the winner by a fixed priority decode after all contenders arrive. Clear bits only after the winner commits, and keep the decode table contiguous so the fast path stays in L1.</how>
  <pitfall>Bitmap width exhaustion or aliasing two resources to one bit creates impossible-to-debug cross-talk.</pitfall>
</pattern>

---

<pattern domain="infrastructure" name="Jittered Lease Renewal">
  <why>Randomizing renewal timing prevents synchronized thundering herds against coordination services.</why>
  <how>Renew leases at a base fraction of TTL plus bounded jitter, cap the earliest renewal to avoid positive feedback, and randomize per client instance with stable seeds. Measure renewal latency separately from work latency so an overloaded coordinator can be detected before mass expiration.</how>
  <pitfall>Using the same seed fleet-wide recreates the herd you were trying to eliminate.</pitfall>
</pattern>

---

<pattern domain="security" name="Parser Depth Fuse">
  <why>Depth fuses stop adversarial nesting from turning a parser into an exponential resource sink.</why>
  <how>Track nesting depth with a hard ceiling, decrement on every recursive descent, and reject before allocating child frames when the remaining budget would underflow. Apply the same budget to arrays, objects, and macro expansion so attackers cannot switch syntactic forms to bypass the fuse.</how>
  <pitfall>Only counting one grammar construct leaves alternate encodings as a bypass path.</pitfall>
</pattern>

---

<pattern domain="database" name="Deferred Secondary Index Repair">
  <why>Decoupling secondary index repair from primary writes shortens commit latency and survives partial failures.</why>
  <how>Write the primary record and an intent log entry in one atomic unit, then enqueue secondary repairs as idempotent jobs keyed by primary version. Rebuild lagging indexes by scanning the intent log, and keep a repair watermark so readers know whether they need a fallback primary lookup.</how>
  <pitfall>Unbounded repair queues can turn a small outage into an index freshness collapse.</pitfall>
</pattern>

---

<pattern domain="math" name="Krylov Restart with Orthogonal Memory">
  <why>Restarting iterative solvers with compressed orthogonal history preserves progress without unbounded memory growth.</why>
  <how>Store a small basis of the last residual directions, orthogonalize the next search vector against that basis, and restart only when the subspace conditioning degrades. Keep the basis numerically reorthogonalized with modified Gram-Schmidt and discard vectors whose contribution falls below a tolerance.</how>
  <pitfall>Poor orthogonalization leaks duplicate directions and negates the restart benefit.</pitfall>
</pattern>

---

<pattern domain="systems" name="NUMA-Affined Hotset Partition">
  <why>Pinning the hotset to the local socket avoids remote memory penalties that dominate under contention.</why>
  <how>Partition allocations by socket, co-locate threads with their hot pages, and periodically rebalance only the cold tail to avoid migration churn. Route inter-socket coordination through a small shared control block, and keep per-socket allocators independent so the allocator metadata does not bounce.</how>
  <pitfall>Blind affinity can strand memory on one socket and starve another under skewed load.</pitfall>
</pattern>

---

<pattern domain="writing" name="Constraint-First Specification">
  <why>Leading with invariants and exclusions makes technical prose easier to implement and review.</why>
  <how>State what the system must never do, then define the exact boundaries, vocabulary, and acceptance criteria before describing any behavior. Use short declarative sentences, prefer measurable terms over adjectives, and keep each paragraph tied to one testable constraint.</how>
  <pitfall>Over-specifying the mechanism can lock the document to an implementation that will soon change.</pitfall>
</pattern>

---

<pattern domain="ml" name="Gradient Noise Scale Probe">
  <why>Measuring gradient noise scale reveals whether batch size is wasting compute or stabilizing learning.</why>
  <how>Estimate the ratio of gradient variance to squared mean across microbatches, then compare it to the effective batch size to find the transition from noise-dominated to signal-dominated training. Adapt accumulation steps when the scale drifts, and log the estimate by layer group rather than only globally.</how>
  <pitfall>Global averages can hide one unstable block that is quietly poisoning convergence.</pitfall>
</pattern>

---

<pattern domain="reasoning" name="Assumption Ledger">
  <why>Making assumptions explicit prevents silent dependency on facts that were never established.</why>
  <how>Write each claim with its required premises, tag premises as observed, inferred, or external, and only advance conclusions whose premises are all satisfied. When a premise fails, invalidate the dependent subtree instead of patching the conclusion in place.</how>
  <pitfall>Unchecked premises accumulate into a tower of brittle conclusions that looks coherent but is not.</pitfall>
</pattern>

---

<pattern domain="coding" name="Idempotent Side-Effect Fence">
  <why>Wrapping effects behind a deduplicating fence makes retries safe under at-least-once execution.</why>
  <how>Compute a stable request fingerprint from semantic inputs, persist the fingerprint before the effect, and treat duplicate fingerprints as no-ops returning the original result handle. Keep the fence store strongly ordered, and separate effect execution from result publication so retries cannot double-commit.</how>
  <pitfall>Fingerprints based on raw payload bytes break when semantically identical requests serialize differently.</pitfall>
</pattern>

---

<pattern domain="state" name="Causal Snapshot Fence">
  <why>Causal fences stop a component from acting on state it has not yet observed in the same causal chain.</why>
  <how>Attach a causal frontier to outbound messages, require inbound state to advance the frontier before consumption, and buffer anything that arrives beyond the current frontier. Merge frontiers monotonically and expose them to downstream components so replay and time travel stay bounded.</how>
  <pitfall>Without a bounded buffer, a future-dated message stream can deadlock the consumer.</pitfall>
</pattern>

---

<pattern domain="infrastructure" name="Probe Separation Window">
  <why>Separating liveness from readiness avoids serving traffic through a process that merely exists but cannot work.</why>
  <how>Use a minimal liveness check that only proves the process is responsive, and a richer readiness check that proves dependencies, warm caches, and quotas are available. Gate rollout on readiness, not liveness, and delay probe activation during cold start with a fixed stabilization window.</how>
  <pitfall>Overloading liveness with dependency checks can cause restart storms during transient outages.</pitfall>
</pattern>

---

<pattern domain="security" name="Secret Zeroization Boundary">
  <why>Defining the exact zeroization boundary prevents secrets from lingering in moved, copied, or swapped buffers.</why>
  <how>Keep secrets in dedicated allocators, lock pages when possible, overwrite with a compiler-resistant primitive, and ensure every copy path is banned or wrapped. Zeroize on explicit release, on panic or exception unwind, and before returning buffers to a pool so ownership transitions are clean.</how>
  <pitfall>Compiler optimization or hidden copies can leave intact replicas even when the visible buffer is scrubbed.</pitfall>
</pattern>

---

<pattern domain="database" name="Page-Local Prefix Compression">
  <why>Compressing keys within a page improves fanout without sacrificing fast in-page binary search.</why>
  <how>Store the first key in full, then encode each subsequent key as common-prefix length plus suffix bytes, and maintain a prefix anchor table for fast reconstruction during search. Rebuild the page when prefix entropy drops, and keep separator keys uncompressed at fence boundaries.</how>
  <pitfall>Excessive compression can make page splits expensive and reverse the expected write savings.</pitfall>
</pattern>

---

<pattern domain="systems" name="Interrupt Budget Governor">
  <why>Governing interrupt work prevents latency-sensitive threads from starvation during burst conditions.</why>
  <how>Assign a processing budget per interrupt source, account consumed cycles, and defer excess work to dedicated queues when the budget is exhausted. Dynamically adjust budgets using observed service latency and queue depth feedback.</how>
  <pitfall>Static budgets often fail under changing workload distributions and device behavior.</pitfall>
</pattern>

---

<pattern domain="ml" name="Representation Collapse Sentinel">
  <why>Early detection of collapsing representations prevents wasted training cycles and degraded generalization.</why>
  <how>Measure feature covariance rank, activation entropy, and pairwise embedding diversity at fixed checkpoints. Alert when diversity metrics decay faster than loss improves and correlate collapse signatures with optimizer state transitions.</how>
  <pitfall>Temporary specialization phases can resemble collapse if measured over too short a horizon.</pitfall>
</pattern>

---

<pattern domain="reasoning" name="Dependency Graph Inversion">
  <why>Reasoning backward from required conclusions exposes hidden assumptions more efficiently than forward chaining.</why>
  <how>Construct a dependency graph from target conclusion to prerequisites, recursively expand unmet nodes, and stop only when all leaves are observable facts or explicit assumptions. Compare the resulting graph against the forward derivation for mismatches.</how>
  <pitfall>Cycles in dependency expansion can masquerade as justification unless detected explicitly.</pitfall>
</pattern>

---

<pattern domain="coding" name="Deferred Destruction Queue">
  <why>Deferring destruction outside critical sections reduces contention and avoids destructor cascades.</why>
  <how>Move retired objects into a thread-local destruction queue, release locks immediately, and perform destruction in batches when outside contention-sensitive regions. Bound queue growth and integrate with reclamation epochs to preserve safety.</how>
  <pitfall>Unbounded queues can accumulate large latent memory spikes under load.</pitfall>
</pattern>

---

<pattern domain="state" name="Transition Proof Record">
  <why>Recording why a transition occurred makes postmortem analysis deterministic instead of speculative.</why>
  <how>Store transition id, triggering event, prerequisite state hash, and rule identifier alongside every mutation. Verify proof records during replay and expose them through audit tooling for causal reconstruction.</how>
  <pitfall>Recording only the result without justification destroys forensic value.</pitfall>
</pattern>

---

<pattern domain="infrastructure" name="Dependency Blast Radius Index">
  <why>Quantifying blast radius identifies changes whose risk exceeds their apparent scope.</why>
  <how>Construct a graph linking services, shared infrastructure, and operational dependencies, then compute weighted reachability scores. Require additional validation for deployments whose blast radius score exceeds historical norms.</how>
  <pitfall>Ignoring transitive dependencies dramatically understates true exposure.</pitfall>
</pattern>

---

<pattern domain="security" name="Privilege Gradient Audit">
  <why>Privilege escalation paths often emerge gradually rather than through a single obvious flaw.</why>
  <how>Model permissions as a graph, compute reachable authority sets from every role, and identify edges whose addition increases reachability disproportionately. Review gradient hotspots before deployment and after policy changes.</how>
  <pitfall>Analyzing only direct permissions misses multi-step escalation chains.</pitfall>
</pattern>

---

<pattern domain="database" name="Snapshot Horizon Compression">
  <why>Compressing adjacent snapshot horizons reduces metadata overhead in MVCC-heavy systems.</why>
  <how>Merge contiguous visibility intervals into horizon segments, store segment references instead of individual transaction ids, and rebuild segments opportunistically during vacuum operations. Preserve exact visibility semantics while reducing storage amplification.</how>
  <pitfall>Incorrect segment merging can expose uncommitted data or hide committed updates.</pitfall>
</pattern>

---

<pattern domain="math" name="Error Amplification Trace">
  <why>Tracing amplification factors pinpoints where numerical error becomes dominant.</why>
  <how>Associate each computation stage with a local amplification estimate, propagate bounds through transformations, and rank stages by cumulative contribution. Prioritize stabilization efforts on the highest contributors rather than globally increasing precision.</how>
  <pitfall>Ignoring correlation between error sources can underestimate total uncertainty.</pitfall>
</pattern>

---

<pattern domain="systems" name="Cross-Core Ownership Bitmap">
  <why>Bitmap ownership tracking reduces synchronization overhead for highly partitionable workloads.</why>
  <how>Assign ownership bits per shard, update ownership only through atomic bitmap transitions, and permit writes solely by the current owner core. Aggregate ownership transfers into batches to amortize coherence traffic.</how>
  <pitfall>Frequent ownership churn can exceed the cost of shared access.</pitfall>
</pattern>

---

<pattern domain="writing" name="Invariant Anchored Documentation">
  <why>Documentation anchored to invariants survives implementation changes better than procedure descriptions.</why>
  <how>State preserved properties first, explain why they matter, then describe mechanisms that enforce them. Reference measurable invariants throughout and update procedures without rewriting the conceptual model.</how>
  <pitfall>Mechanism-heavy documents become obsolete whenever implementations evolve.</pitfall>
</pattern>

---

<pattern domain="ml" name="Attention Sink Detection">
  <why>Detecting sink tokens identifies wasted attention capacity and degraded context utilization.</why>
  <how>Measure cumulative attention mass received by each token position, identify persistent disproportionate recipients, and compare sink behavior across prompt classes. Adjust architecture or training objectives when sink concentration exceeds expected bounds.</how>
  <pitfall>Special structural tokens may appear sink-like while serving legitimate functions.</pitfall>
</pattern>

---

<pattern domain="reasoning" name="Orthogonal Evidence Check">
  <why>Independent evidence sources reduce the risk of reinforcing the same underlying error.</why>
  <how>Classify evidence by origin, mechanism, and dependency chain, then require support from sources whose failure modes are minimally correlated. Discount confirmations that ultimately derive from the same root assumption.</how>
  <pitfall>Multiple reports from a common source can create a false appearance of consensus.</pitfall>
</pattern>

---

<pattern domain="coding" name="Structural Equality Firewall">
  <why>Separating structural equality from identity prevents subtle cache and synchronization bugs.</why>
  <how>Implement explicit structural comparison routines, forbid implicit identity fallbacks, and document equality semantics at API boundaries. Use identity only for ownership and lifecycle concerns.</how>
  <pitfall>Mixing equality notions causes nondeterministic behavior across components.</pitfall>
</pattern>

---

<pattern domain="state" name="Event Horizon Marker">
  <why>Explicit horizons define how far history remains mutable and replayable.</why>
  <how>Periodically emit immutable horizon markers, prohibit mutation before the latest stable horizon, and use horizons as recovery anchors. Compress older history beyond the horizon into summarized forms.</how>
  <pitfall>Moving horizons prematurely can destroy information needed for recovery.</pitfall>
</pattern>

---

<pattern domain="infrastructure" name="Correlated Failure Simulator">
  <why>Simulating correlated faults reveals vulnerabilities invisible to independent-failure testing.</why>
  <how>Inject simultaneous dependency failures according to observed incident correlations, measure service degradation, and compare outcomes against resilience objectives. Maintain correlation models from production telemetry.</how>
  <pitfall>Independent-failure assumptions consistently underestimate real-world outage severity.</pitfall>
</pattern>

---

<pattern domain="security" name="Context-Bound Authentication">
  <why>Binding authentication to execution context prevents token reuse in unintended environments.</why>
  <how>Include device, workload, audience, and execution-environment identifiers in authentication proofs, validate all bindings before authorization, and invalidate tokens when contextual attributes change.</how>
  <pitfall>Weak context definitions allow attackers to replay tokens across similar environments.</pitfall>
</pattern>

---

<pattern domain="database" name="Visibility Bitmap Acceleration">
  <why>Visibility bitmaps reduce repeated version checks during large scans.</why>
  <how>Maintain page-level visibility summaries, update them transactionally alongside tuple state changes, and consult summaries before inspecting individual records. Recompute conservatively during recovery.</how>
  <pitfall>Stale visibility summaries can silently violate snapshot correctness.</pitfall>
</pattern>

---

<pattern domain="math" name="Residual Orthogonality Monitor">
  <why>Loss of residual orthogonality often signals impending solver inefficiency or instability.</why>
  <how>Track pairwise residual inner products across iterations, quantify orthogonality decay, and trigger reorthogonalization when thresholds are exceeded. Correlate decay with convergence slowdown to validate interventions.</how>
  <pitfall>Ignoring orthogonality loss can produce misleading convergence diagnostics.</pitfall>
</pattern>

---

<pattern domain="systems" name="Queue Depth Derivative Alarm">
  <why>Depth growth rate predicts overload earlier than absolute queue length.</why>
  <how>Monitor first and second derivatives of queue depth, establish workload-specific baselines, and alert on sustained positive acceleration. Couple alarms to automated throttling or scaling responses.</how>
  <pitfall>Transient bursts can resemble overload unless trend duration is considered.</pitfall>
</pattern>

---

<pattern domain="writing" name="Claim Traceability Matrix">
  <why>Traceable claims make technical documents auditable and maintainable over time.</why>
  <how>Assign identifiers to claims, link each to supporting evidence, requirements, and tests, and update references whenever revisions occur. Maintain bidirectional navigation between claims and proofs.</how>
  <pitfall>Unlinked claims accumulate and become impossible to validate systematically.</pitfall>
</pattern>