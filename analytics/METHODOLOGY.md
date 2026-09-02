# Outcome-disparity screening: method, limits, and what would be required to go further

**This module is a screening methodology demonstration. It is not a finding about any
company, and nothing in it establishes that any company treated anyone unfairly.**

Disparity screening flags cells for investigation. It is the first step of a process whose
later steps — individual-level data, matched comparison groups, a search for less
discriminatory alternatives — are not possible with public CFPB data and are not attempted
here. Companies are named in the code and the output because the data names them; no
conclusory claim about any of them appears anywhere in this repository.

## What is computed

For each (company, product) cell with at least 50 resolved complaints:

- **relief rate** — closed with monetary or non-monetary relief, against closed with
  explanation only
- **timely-response rate**

each with a **Wilson interval**, compared against the same product at every *other*
company by a **two-proportion z-test**, with **Benjamini-Hochberg** applied across the full
family of tests.

Three choices carry the statistics, and each is a judgement stated rather than hidden:

**The baseline excludes the cell under test.** Comparing a large issuer against a product
baseline that includes its own complaints tests it against a number it helped produce, and
biases every such test toward finding nothing.

**Benjamini-Hochberg rather than Bonferroni.** At this stage a false positive costs an
analyst an afternoon and a false negative misses the thing the screen exists to find.
Controlling the false discovery rate is the right trade; controlling family-wise error over
several hundred tests would leave the screen unable to flag anything.

**A minimum cell size of 50, applied before testing.** 1,124 companies x 4 products is
mostly cells of a handful of complaints, where the z-test's normal approximation does not
hold. Filtering after computing p-values would still let those cells inflate the
correction and suppress real signal elsewhere.

**Not computed: the consumer-dispute rate.** CFPB stopped publishing `consumer_disputed` in
April 2017 and the field is absent from this extract.

## Why the peer group is wrong (the module's own headline limitation)

Run against the **relief rate**, this screen flags **52 of 80 cells — 65%**.

A screen that flags two-thirds of what it tests is not detecting anomalies. The natural
suspicion is a power artifact: with enough complaints, trivial differences reach
significance. That is not what is happening here. Among flagged cells the median absolute
difference from baseline is **0.158** — a sixteen percentage-point gap — against **0.047**
among unflagged, and median cell size is similar between the two groups (128 vs 94). The
effects are large and real.

What is wrong is the comparison group. CFPB's product taxonomy has four values, and
"Debt collection" contains national banks, debt buyers, and credit bureaus — businesses
whose *role* in a complaint differs so fundamentally that a common relief rate is not a
meaningful expectation. TransUnion's relief rate under Debt collection is 0.71 against a
0.18 baseline; that is a statement about what a credit bureau does when a consumer disputes
a record, not evidence about its conduct.

The same screen on the **timely-response rate** flags **6 of 80 — 8%**, with
Benjamini-Hochberg removing 13 of 19 raw hits. Timeliness is a procedural obligation that
means the same thing for every firm regardless of business model, so the peer group is
valid and the screen behaves like a screen.

**That contrast is the substance of this module.** The same statistics, on the same cells,
produce a usable screen for one outcome and an unusable one for the other, and the
difference is entirely in whether the comparison group is a genuine peer group. Fixing it
requires a firm-type stratification the public data does not contain.

## Limitations that would remain even with a correct peer group

**Ecological inference.** CFPB public data contains no individual race or ethnicity — the
finest geography is a 3-digit ZIP prefix. Any demographic association computed from it is
**area-level and cannot support individual-level conclusions**. A ZIP3 whose complaints
resolve less favourably tells you nothing about how any individual in it was treated. This
is the ecological fallacy, and it is the single most common way analyses like this are
misused.

**Selection bias.** Complaints are self-selected, not a random sample of consumer
experience. Propensity to complain to a federal regulator varies with geography, product,
income, and financial literacy — all of which correlate with the demographics such an
analysis would examine. The sample is shaped by the same variables as the question.

**Confounding.** Product mix, customer tenure, underwriting standards, and servicing
practices differ across companies. Raw rate differences are **not causal estimates** and
cannot be read as such.

**Outcome coding.** "Closed with explanation" versus "closed with relief" is the company's
own characterisation of what it did, not an adjudicated outcome.

## The geographic layer, and why it is not built

The build guide suggests joining ZIP-prefix complaint rates to ACS demographics at the
ZCTA level. That join is **not implemented**, and the reason is worth stating rather than
leaving as an omission: it would produce exactly the area-level demographic association
whose limits the ecological-inference paragraph above describes, and it would be the most
misreadable output in the repository. Building it responsibly requires the framing and the
caveats to travel with every number, which a CSV does not do.

The ZIP3 field is derived in `load()` and available for that work when it can be done
properly.

## Regulatory context

**SR 11-7 (Supervisory Guidance on Model Risk Management).** This module is not a model in
the SR 11-7 sense — it makes no predictions and drives no decisions — but the guidance's
framing applies to how its output should be treated. SR 11-7 requires that model limitations
be documented and that outputs be used with an understanding of those limitations, and it
places effective challenge at the centre of model risk management. The 65% flag rate above
is what effective challenge of this screen produces: the method is sound and the application
is invalid for that outcome.

**ECOA / Regulation B.** ECOA prohibits discrimination in any aspect of a credit
transaction, and Regulation B implements it. Disparity screening is a standard first step
in fair-lending analysis, but a disparity is not a violation: Reg B contemplates legitimate
business justification, and establishing disparate impact requires showing that a specific,
identifiable practice causes the disparity and that a less discriminatory alternative
exists that serves the same business need.

**What a model-risk or fair-lending reviewer would demand next**, none of which public
complaint data supports:

1. **Individual-level data** with protected-class information, or a defensible proxy
   methodology such as BISG, with its own error rates carried through the analysis.
2. **Matched comparison groups** — controlling for product, credit profile, tenure,
   geography, and channel, so that compared consumers are actually comparable.
3. **A specific practice identified** as the mechanism, rather than an aggregate rate gap.
4. **A less-discriminatory-alternative search**, documented, including alternatives
   considered and rejected and why.
5. **Adverse-action reason codes** traced to the decisions in question.
6. **Documented effective challenge** by a party independent of whoever built the analysis.

Knowing which of these are missing — and that the module cannot substitute for them — is
the point of the module.
