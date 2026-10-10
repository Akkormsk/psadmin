# PSADMIN architecture

## Product

PSADMIN automates tender intake, analysis, commercial calculation, execution and financial work. Its main domains are tenders, order calculations, counterparties and capabilities, provider price knowledge, internal calculators, finance, payroll and financial accounting.

## Navigation

The home page is the top-level launcher for independent applications. Global functions are **Data**, Assistant and Settings. The shared base template supplies navigation with Back, current heading and Home: Back uses same-origin history when available, otherwise its logical parent/Home fallback. A business object opens in the current application's workspace; a URL remains available for deep links, refresh and browser history, but ordinary navigation must not create a duplicate page or global shell.

`Data` is the visible entry to the shared registry: counterparties, `ProcessDefinition` capabilities, and provider prices/calculators. A counterparty can have several roles; do not add a parallel provider registry. The production-base endpoint remains JSON used by the tender route drawer.

## V1 and V2

V1 tender routes and the existing internal calculator remain live and must not be removed. V2 has conversation/context records, provider knowledge versions, structured price-rule calculators, the provider workspace, and the beginning of the V2 calculation chain. V2 is not accepted for production rollout and its full calculation pipeline is not yet integrated end-to-end.

## Calculation pipeline

The target flow is `TenderSourceItem -> TenderCommercialItem -> CalculationComponent -> Route/ProcessDefinition -> Counterparty -> ProviderCalculatorBinding -> ProviderCalculationQuote -> TenderComputeLine.result`. A provider that performs an all-in-one service must not force artificial intermediate steps. Natural-language interpretation is separate from deterministic validation and calculation.

## Provider system

`Counterparty` is the organisation. `ProcessDefinition` is a capability; `StageCounterpartyLink` joins it to a counterparty. `ProviderKnowledgeStaging` is temporary ingestion input. `CounterpartyKnowledgeVersion` stores a versioned normalized price source. `ProviderCalculatorBinding` selects its calculator; `ProviderCalculationQuote` preserves a result. `calculate_provider(...)` is the structured-rules calculation service.

Only a confirmed knowledge version may activate a structured binding. Raw uploads are purged after confirmation. Superseded knowledge stays recoverable for old quotes; the provider UI hides it by default. The Prices tab expands a version into its stored structured rows, not raw JSON or another AI pass. Sewing XLS currently provides 18 parsed variants; the verified example is 100 classic women's jersey T-shirts at 414.80 RUB each, total 41,480 RUB.

## Assistant and learning

The global assistant drawer has persistent conversations, page-context snapshots, a backend tool registry, provider draft/upload/confirmation and calculator opening. Its declared capabilities must match registered tools. It does not yet implement a universal data agent.

Existing learning data includes `OwnerInteraction`, `OwnerFeedbackEvent`, `KnowledgeRecord`, `Lesson`, `ProductionTrainingExample` and `Proposal`. Keep their scopes distinct and versioned; do not replace them with another feedback store.

## Cache and integrations

V2 cache preparation includes `TenderComputeJob`, `TenderComputeWorkUnit`, `TenderComputePreparation`, `Step4DecisionCache`, background eligibility, deduplication and cache reuse. Full calculation aggregation remains partial. Oasis/Gifts adapters and the legacy Cascade exist; Project111 and FSPrint are not accepted integrations. Do not infer a working integration from an adapter class alone.

## Roadmap

1. **PARTIAL** Navigation and UX acceptance: Data entry and structured price viewing are done; authenticated visual acceptance and a reusable navigation header remain.
2. **NOT STARTED** Universal Data Agent: bounded read-only analysis and preview/confirmation mutations via the existing registry and learning models.
3. **PARTIAL** Automatic V2 calculation: complete routing, matching, calculator execution, aggregation, clarification/resume and cache correctness.
4. **PARTIAL** Background warming; no uncontrolled AI backfill.
5. **PARTIAL/NOT STARTED** External sources: Oasis, Project111, FSPrint.
6. **PARTIAL** Route conversation/learning, then Telegram with the same backend.
