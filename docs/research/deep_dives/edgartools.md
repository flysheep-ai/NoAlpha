# EdgarTools Deep Dive

## Repository

https://github.com/dgunning/edgartools

Current activity observed locally: `bd77f41 2026-06-29 test(sections): update 1ho 8-K test for SIGNATURES named section (GH #879)`.

## Architecture

Python library for SEC EDGAR filings, entity lookup, company facts, XBRL, statements, funds, ownership, and local/cloud storage.

## Core Modules

- `edgar/__init__.py`: public API facade.
- `edgar/_filings.py`: filing indexes, filing objects, filters.
- `edgar/httprequests.py`: HTTP retry, throttling, SEC 429 handling.
- `edgar/entity/entity_facts.py`: companyfacts parsing and AI-ready facts.
- `edgar/entity/statement.py`: financial statement wrapper and formatting.
- `edgar/financials.py`: XBRL extraction and standardized financial access.
- `edgar/xbrl/core.py`: XBRL utilities and date/period handling.
- `edgar/xbrl/concept_graph.py`: concept graph over XBRL tags and values.

## Important Source Files

- `edgar/httprequests.py`: `max_requests_per_second = 8`, retry constants, `TooManyRequestsError`.
- `edgar/entity/entity_facts.py`: `download_company_facts_from_sec`, local storage path, LRU cache.
- `edgar/financials.py`: `Financials.extract(filing)` and standard statement accessors.
- `edgar/xbrl/concept_graph.py`: `ConceptGraph` and `Concept` navigation.

## Execution Flow

Typical flow: find company/entity -> fetch submissions/filings -> parse XBRL/companyfacts -> expose statements/facts as Python objects/DataFrames.

## Data Flow

SEC API/filings -> HTTP client with retry/rate limit -> local/cloud storage optional -> entity facts/XBRL parser -> statements/facts.

## Strengths

- Handles SEC-specific operational issues better than a naive client.
- Has explicit rate-limit protection and 429 messaging.
- Supports local storage and cache.
- Rich XBRL and statement abstractions.

## Weaknesses

- It is not a PIT financial warehouse by itself.
- Companyfacts current API can include restatements/latest state; backtesting needs snapshot discipline.
- Business metrics such as non-GAAP, consensus, guidance surprise remain outside scope.

## Production Readiness

High for SEC ingestion/parsing library, medium for direct production dependency until load tests and API boundaries are verified.

## What We Should Reuse

- SEC HTTP/rate-limit practices.
- XBRL/companyfacts extraction.
- Filing and entity abstractions.

## What We Should Reimplement

- PIT storage.
- Source snapshot metadata.
- Analyst-ready metric normalization.

## What We Should Avoid

- Assuming SEC structured facts are directly comparable across companies without taxonomy/segment/period checks.

