# Career Goals

<!-- This file is your ground truth. Every LLM judgment in Mission Control
     (job fit, role category, content radar) reads this alongside your resume.
     Be specific - vague goals produce vague verdicts. -->

## Target Roles
*(In descending priority order)*

1. <!-- e.g. Senior Data Scientist, Marketing Analytics -->
2.
3.

## Key Technical Areas
*(Core skills / domains you want roles to match on)*

- <!-- e.g. Causal inference -->
- <!-- e.g. Experimentation frameworks -->

## Domain Expertise

<!-- 2-4 sentences: what you actually do, at what scale, for whom. This is the
     paragraph the model leans on most for a "does this role fit" judgment. -->

## Target Keywords
*(It scores roles, it scores how well your public activity -
  GitHub/LinkedIn/BlueSky - aligns with these goals, and it generates the
  Indeed/LinkedIn/Built In searches. A match anywhere in a role's TITLE or
  DESCRIPTION RAISES its score; missing them all does not disqualify a role, it
  just scores low. The only threshold is rules.min_match_score in
  config/job-sources.yaml.)*

<!-- ORDER MATTERS for job search: only the first `max_queries` terms (8 by
    default, set per aggregator) become board searches. The profile scanners
    read the whole list regardless of order, so the strongest job-title terms
    lead and the narrow method names follow. -->

- <!-- e.g. causal inference -->
- <!-- e.g. marketing mix modeling -->

## Exclude Keywords
*(Hard stop. A match in a role's TITLE ONLY - never the description - drops the
  posting immediately: it never enters the tracker and never costs an LLM call.
  Each term is also sent to the job boards that support it as `-term` so the
  noise is filtered before download. Matching
  is case-insensitive and catches plurals ("intern" kills "Interns"), but only
  whole words - "internal" and "international" are safe. Title-only on purpose:
  a description that merely mentions mentoring interns is not an intern role.)*

- <!-- e.g. junior -->
- <!-- e.g. intern -->

## Career Positioning
*(How you want to be perceived professionally. Free text - no scanner parses
  this; it goes to the model as part of your ground truth, so it shapes the
  fit reads and the content angles.)*

- <!-- e.g. Measurement leader who ships production systems -->
- <!-- e.g. Bridge between causal inference theory and commercial impact -->

## Notes
*(Anything else the model should weigh - emphases, gaps you are closing,
  constraints. Also free text.)*

- <!-- e.g. Focus on shipping production systems, not just research -->
- <!-- e.g. Clean rooms experience is a gap to address -->
