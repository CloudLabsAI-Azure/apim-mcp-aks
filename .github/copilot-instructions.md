# Copilot Instructions

## Project Overview

This project implements an **APIM + MCP + AKS** architecture for a Healthcare Digital Quality Management **Next Best Action (NBA) Agent**.

## Domain Knowledge

### Meridian Health Partners Quality Protocol (MHP-QP-v3.2.1)

The agent operates under the **MHP Quality Protocol**, a proprietary domain knowledge document at `facts/mhp_quality_protocol.json`. Key protocol elements:

- **MHP Quality Score Formula**: `MHP_QS = (0.35 × HEDIS) + (0.25 × CAHPS) + (0.20 × Rx_PDC) + (0.15 × Preventive) + (0.05 × Readmission_Inv)`. Target: 82.0.
- **HEDIS Measure Weights**: COL=2.1, BCS=1.8, CDC-H=1.5, CBP=1.3, SPC=1.1, DSF=1.0, WCV=0.9, IMM=0.8
- **Risk Tiers**: Tier 1 (≥0.85 AND ≥3 gaps → 24hr care manager), Tier 2 (0.65-0.84 OR ≥2 gaps → weekly), Tier 3 (0.40-0.64 AND 1 gap → monthly), Tier 4 (<0.40 AND 0 gaps → quarterly)
- **Outreach Cadence**: Day 0 Portal → Day 3 SMS → Day 7 Phone → Day 14 Phone+Mail → Day 21 In-Person (Tier 1&2) → Day 30 Escalation
- **Provider Tiers**: Platinum ≥90% ($2,500/qtr), Gold 80-89% ($1,000/qtr), Silver 70-79% (coaching), Bronze <70% (QIP in 30 days)
- **Cost-Effectiveness**: Portal $0.50/12%/5.1:1, SMS $0.75/18%/4.2:1, Phone $8.50/34%/3.2:1, Coordinator $45/62%/1.8:1, Home $125/78%/1.2:1
- **Priority Algorithm**: `Priority = (Weight×10) + (Risk×5) + (Days×-0.1) + (Prob×3)`. Top 20% auto-approved, middle 60% coordinator, bottom 20% deferred.
- **MES Formula**: `MES = (Portal×0.3) + (Attendance×0.4) + (Response×0.3)`. High ≥0.70, Medium 0.40-0.69, Low <0.40.

## Evaluation Framework

The agent uses 5 evaluators via Azure AI Foundry Evaluation SDK:

| Evaluator | Metric | Scale | Purpose |
|-----------|--------|-------|---------|
| IntentResolutionEvaluator | intent_resolution | 1-5 | Correct intent identification |
| ToolCallAccuracyEvaluator | tool_call_accuracy | 1-5 | Correct tool selection |
| TaskAdherenceEvaluator | task_adherence | flagged T/F | Response follows task |
| GroundednessEvaluator | groundedness | 1-5 | Response grounded in MHP context |
| RelevanceEvaluator | relevance | 1-5 | Response relevant to query |

GroundednessEvaluator requires `context` (MHP protocol excerpt). RelevanceEvaluator requires `query` and `response`.

## Architecture

- **Server**: `src/next_best_action_agent.py` — FastAPI MCP Server on AKS
- **Evaluations**: `evals/run_evaluations.py` — Client-side eval runner
- **Eval Data**: `evals/healthcare_digital_quality/healthcare_digital_quality_eval_data.jsonl`
- **Domain Knowledge**: `facts/mhp_quality_protocol.json`
- **Spec**: `.speckit/specifications/healthcare_digital_quality_agent.spec.md`
- **Infra**: `infra/` — Bicep IaC for Azure resources

## Fine-Tuning Strategy

Fine-tuning embeds MHP protocol knowledge into the model so it can produce grounded responses without external context retrieval. The base model (`gpt-4o-mini`) gives generic HEDIS answers; the fine-tuned model uses exact MHP formulas, weights, and tiers.

### Pre vs Post Evaluation

1. **Pre-fine-tuning**: `USE_TUNED_MODEL=false` → base model → generic answers → LOW groundedness
2. **Post-fine-tuning**: `USE_TUNED_MODEL=true` → fine-tuned model → MHP-specific answers → HIGH groundedness
