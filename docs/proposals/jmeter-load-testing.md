# Proposal: JMeter Integration for Load Testing

**Status**: Deferred  
**Priority**: Low  
**Parent Epic**: [FLPATH-4036](https://redhat.atlassian.net/browse/FLPATH-4036)

## Summary

Evaluate and potentially integrate Apache JMeter for heavy load and sustained stress testing of Cost On-Prem APIs, complementing the existing pytest-based performance test suite.

## Background

The current performance testing approach uses pytest with Python's `concurrent.futures.ThreadPoolExecutor` for concurrent API load simulation. This works well for moderate concurrency (5-20 users) but may not scale for:

- Heavy load simulation (50-100+ concurrent users)
- Sustained soak testing over multiple hours/days
- Distributed load generation across multiple machines
- Industry-standard reporting for stakeholder communication

## Proposed Work

### Phase 1: Evaluation (2-3 days)

1. Create proof-of-concept JMeter test plan for API-002 (Report API under load)
2. Compare results with existing pytest implementation
3. Evaluate CI integration options (Jenkins plugin, CLI execution)
4. Document findings and recommendation

### Phase 2: Implementation (if approved)

1. Create JMeter test plans for:
   - PERF-API-002: Report API under load (10-50 concurrent users)
   - PERF-SCALE-004: Concurrent API queries
   - PERF-SOAK-001: Continuous operation (extended duration)
2. Integrate JMeter execution into CI pipeline
3. Add JMeter report parsing to HTML report generator
4. Document test execution procedures

---

## Current vs. Proposed Approach

| Capability | Current (pytest) | Proposed (JMeter) |
|------------|------------------|-------------------|
| Concurrent users | ThreadPoolExecutor (5-20) | Native thread groups (100+) |
| Distributed load | Single machine | Multi-machine coordinated |
| Ramp-up patterns | Manual implementation | Built-in configurable |
| Think times | `time.sleep()` | Native support |
| Protocol support | HTTP only | HTTP, JDBC, JMS, etc. |
| CI integration | pytest/JUnit XML | Jenkins plugin, CLI |
| Learning curve | Python (team knows) | JMeter (new tool) |

---

## Decision Criteria

Proceed with JMeter integration if ANY of the following become true:

- [ ] Need to simulate 50+ concurrent users reliably
- [ ] Need distributed testing across multiple machines
- [ ] Stakeholders specifically request JMeter/industry-standard reports
- [ ] Current pytest approach shows reliability issues at scale
- [ ] Soak tests (7-day runs) prove difficult to manage in pytest

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Dual tooling increases maintenance | Only use JMeter for tests where pytest is insufficient |
| Team unfamiliar with JMeter | Provide training, document test plans thoroughly |
| Auth complexity (JWT tokens) | Use JMeter HTTP Header Manager with token refresh |
| Data generation integration | Keep NISE in pytest, JMeter only for pure API load |

---

## Recommendation

**Defer this work.** The current pytest-based approach adequately covers the FLPATH-4036 requirements:

- Baseline and scale tests complete successfully
- Concurrency needs (5-20 users) are within pytest capabilities
- HTML reporting meets visibility requirements
- Team can iterate quickly with familiar Python tooling

Revisit this proposal when:
- Moving to production-scale testing with larger customer profiles
- Soak testing requirements demand multi-day unattended execution
- External stakeholders require JMeter-format reports

---

## Acceptance Criteria (if implemented)

- [ ] JMeter test plans for API-002, SCALE-004, SOAK-001
- [ ] CI job executes JMeter tests on demand
- [ ] JMeter results integrated into HTML performance report
- [ ] Documentation for running JMeter tests locally and in CI
- [ ] Comparison report: pytest vs JMeter results for same test

---

## References

- [Apache JMeter](https://jmeter.apache.org/)
- [JMeter Best Practices](https://jmeter.apache.org/usermanual/best-practices.html)
- [JMeter Jenkins Plugin](https://plugins.jenkins.io/performance/)
- Current performance tests: `tests/suites/performance/`
- Performance plan: `docs/performance/performance-testing-plan.md`
