# Spare Parts Stock Intelligence: Mathematical & Operational Methodology

This document details the mathematical formulations, statistical classification rules, calibration dynamics, and simulation architecture implemented in the **Spare Parts Stock Intelligence Engine**.

---

## 1. System Overview & Problem Formulation

In authorized luxury automobile after-sales workshops (e.g., Mercedes-Benz, BMW, Porsche), spare parts inventory operations suffer from two compounding systemic failures:
1. **The Lead-Time Variability Trap**: Over 60% of parts catalog lines are imported with volatile overseas lead times (7 to 45 days). Standard DMS (Dealer Management System) static min-max rules ignore lead-time variance and booking forecasts, causing frequent shelf stock-outs that necessitate emergency air-freight orders at **35% to 50% OEM surcharges**.
2. **The Phantom Stock Loop**: Workshop technicians frequently request fast-moving parts verbally at the dispatch counter without an open Job Card. When counter staff issue parts without entering them into the DMS, the shelf stock decrements physically, but the ERP book stock remains artificially high:
   $$\text{Phantom Stock}_i(t) = \max\Big(0, \text{BookStock}_i(t) - \text{PhysicalStock}_i(t)\Big)$$
   Because automated replenishment logic inspects the ERP book stock, **reorders fail to trigger** when $\text{BookStock}_i(t) > \text{ROP}_i$ even though $\text{PhysicalStock}_i(t) = 0$. When the next scheduled vehicle arrives on the hoist, the part is missing, stranding customer vehicles and triggering punitive emergency air orders.

---

## 2. Multi-Dimensional Classification Engine (`src/classify.py`)

Every catalog SKU $i \in \{1, \dots, N\}$ is classified across three orthogonal dimensions: Annual Value (ABC), Demand Predictability (XYZ), and Operational Criticality.

### 2.1 Consumption Metrics
From 365 days of historical workshop demand, we compute:
- **Mean Daily Demand**:
  $$\mu_{i} = \frac{1}{365} \sum_{t=1}^{365} d_{i,t}$$
- **Daily Demand Variance & Standard Deviation**:
  $$\sigma_{i}^2 = \frac{1}{365} \sum_{t=1}^{365} (d_{i,t} - \mu_i)^2, \quad \sigma_i = \sqrt{\sigma_{i}^2}$$
- **Annual Consumption Value ($ACV$)**:
  $$ACV_i = \mu_i \times 365 \times \text{UnitCost}_i$$

### 2.2 ABC Classification (Pareto Value Distribution)
SKUs are sorted in descending order of $ACV_i$, and cumulative value share $C_i$ is evaluated:
$$C_i = \frac{\sum_{k=1}^i ACV_k}{\sum_{j=1}^N ACV_j}$$
- **Class A**: $C_i \le 0.80$ (Top 80% of annual value, ~10-15% of SKUs)
- **Class B**: $0.80 < C_i \le 0.95$ (Next 15% of annual value, ~20-25% of SKUs)
- **Class C**: $C_i > 0.95$ (Remaining 5% of annual value, ~60-70% of SKUs)

### 2.3 XYZ Classification (Demand Predictability)
We evaluate the Coefficient of Variation ($CV_i$):
$$CV_i = \begin{cases} \dfrac{\sigma_i}{\mu_i}, & \text{if } \mu_i > 0 \\ \infty, & \text{if } \mu_i = 0 \end{cases}$$
- **Class X (Regular/Predictable)**: $CV_i < 0.50$
- **Class Y (Moderate Variability)**: $0.50 \le CV_i < 1.00$
- **Class Z (Highly Intermittent / Erratic)**: $CV_i \ge 1.00$

### 2.4 Composite Policy Classes & Target Service Levels
SKUs are mapped into three operating policy classes:
1. **`FAST`**: High-turnover predictable parts ($A \text{ or } B$ and $X \text{ or } Y$).
   - Target Service Level: **97.0%** ($Z = 1.88$)
2. **`CRITICAL_SLOW_IMPORTED`**: Parts with Criticality tier 3 or imported status ($\text{is\_imported} = 1$) that do not qualify as `FAST`.
   - Target Service Level: **99.0%** ($Z = 2.33$) + Sister-centre pooling eligibility.
3. **`LONG_TAIL`**: Remaining catalog items (low velocity, non-critical domestic).
   - Target Service Level: **90.0%** ($Z = 1.28$)

---

## 3. Stocking Policy Engine (`src/policy.py`)

### 3.1 Dynamic Safety Stock ($SS$)
Safety stock protects against stochastic lead-time demand fluctuations:
$$SS_i = \left\lceil Z_i \times \sigma_{d,i} \times \sqrt{LT_i} \right\rceil$$
Where:
- $Z_i$ is the standard normal quantile for the target service level.
- $\sigma_{d,i}$ is daily demand standard deviation.
  *Fallback for intermittent parts*: If $\sigma_{d,i} = 0$ but $\mu_i > 0$, Poisson approximation is applied: $\sigma_{d,i} = \sqrt{\mu_i}$.
- $LT_i$ is the supplier replenishment lead time in days.

### 3.2 Dynamic Reorder Point ($ROP$)
The Reorder Point incorporates expected lead-time demand plus safety stock:
$$ROP_i = \left\lceil \mathbb{E}[D_{LT,i}] + SS_i \right\rceil$$

Where $\mathbb{E}[D_{LT,i}]$ uses a blended booking-driven forecast:
$$\mathbb{E}[D_{LT,i}] = w \cdot D_{LT,i}^{\text{booked}} + (1 - w) \cdot (\mu_i \times LT_i)$$
- $w$ is the forecast blend weight (configured to $0.70$).
- $D_{LT,i}^{\text{booked}}$ explodes the workshop forward service appointment calendar over the upcoming $LT_i$ days.

### 3.3 Order-Up-To Maximum Level ($Max$)
$$Max_i = \left\lceil ROP_i + \mu_i \times \text{ReviewPeriod} \right\rceil$$
Where $\text{ReviewPeriod}$ is configured to 14 days. Minimum stock level is bounded by safety stock ($Min_i = SS_i$).

### 3.4 Worked Numerical Benchmark
For a benchmark critical part with:
- Daily Demand $\mu = 1.2$ units/day
- Replenishment Lead Time $LT = 12$ days
- Daily Demand Std Dev $\sigma_d = 0.8$
- Service Level: 99.0% ($Z = 2.33$)
- Review Period: 14 days

**Calculations**:
1. Safety Stock:
   $$SS = \lceil 2.33 \times 0.8 \times \sqrt{12} \rceil = \lceil 6.457 \rceil = \mathbf{7 \text{ units}}$$
2. Lead Time Demand:
   $$D_{LT} = 1.2 \times 12 = 14.4 \text{ units}$$
3. Reorder Point:
   $$ROP = \lceil 14.4 + 7 \rceil = \lceil 21.4 \rceil = \mathbf{22 \text{ units}}$$
4. Max Level:
   $$Max = \lceil 22 + 1.2 \times 14 \rceil = \lceil 38.8 \rceil = \mathbf{39 \text{ units}}$$
*Property Verified*: $ROP \ge SS$ is guaranteed for all parameter regimes.

### 3.5 Sister-Centre Inter-Branch Pooling
To avoid holding excessive luxury stock at every dealership branch, three sister centres share critical inventory:
- When a stock-out occurs on a `CRITICAL_SLOW_IMPORTED` SKU:
  1. The system first checks inventory across the 3 sister centres.
  2. If stock exists in any sister branch, an **Inter-Branch Transfer (IBT)** is initiated (24-hour transit, 5% transfer handling surcharge).
  3. Only if all sister centres are out of stock does the system escalate to an OEM emergency air-freight order (35-50% surcharge).

---

## 4. Traceability Ledger & Gated Architecture (`src/ledger.py`)

The inventory ledger is implemented on SQLite using an immutable, append-only event structure:

```sql
CREATE TABLE ledger_events (
    event_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    sku_id TEXT NOT NULL,
    qty INTEGER NOT NULL,
    event_type TEXT NOT NULL, -- 'RECEIPT', 'BIN_PUTAWAY', 'ISSUE', 'RETURN', 'COUNT_ADJUST'
    job_card_id TEXT,
    technician_id TEXT,
    counter_user TEXT,
    book_qty_after INTEGER,
    physical_qty_after INTEGER
);
```

### 4.1 Operating Modes
- **`LEGACY` Mode**:
  - Counter staff are permitted to issue parts verbally without a Job Card.
  - A configurable share ($\sim 15\%$) of unlinked issues are not recorded in the DMS, creating shrinkage and phantom stock.
- **`GATED` Mode**:
  - Unlinked verbal issues are strictly blocked by the counter terminal.
  - If a technician arrives in an emergency without a final Job Card, a **4-Hour Provisional Pass** (`PROV-XXXXXX`) can be issued. If not reconciled against an active Job Card within 4 hours, an automated alert flags the Workshop General Manager.

### 4.2 Scheduled Cycle Counts
To prevent phantom stock persistence, scheduled counts dynamically sync book to physical:
1. **Daily Top-50 Risk Counts**: Ranked by Annual Value $\times$ Theft Vulnerability:
   $$\text{RiskScore}_i = ACV_i \times \text{TheftRiskFactor}_i$$
2. **Weekly A-Class Counts**: High-velocity A-Class parts verified every 7 days.

---

## 5. Simulation & Calibration Engine (`src/simulate.py`)

### 5.1 Baseline Calibration Approach
To evaluate the true economic ROI, baseline parameters are calibrated (tuning input operating behaviors, not outcomes) so that the simulated dealership lands on empirical luxury dealership metrics:
- **Target Emergency Order Rate**: $22\% \pm 2\%$
- **Target Value Shrinkage**: $3.0\% \pm 0.5\%$ of average inventory value

**Mechanisms Tuned**:
1. `stock_factor`: Controls the opening stock buffer fraction, simulating legacy under-stocking.
2. `static_coverage_days`: Reflects static min-max rules (e.g. 0.6 days of demand buffer) that ignore overseas 45-day lead times.
3. `legacy_untracked_share`: The percentage of verbal counter requests issued without DMS entry.

### 5.2 Monte Carlo Simulation Pipeline
1. **Demand Generation**: Vectorized stochastic daily demand matrix $\mathbf{D} \in \mathbb{N}^{365 \times 5000}$ sampled from SKU Poisson distributions:
   $$D_{t,i} \sim \text{Poisson}(\mu_i)$$
2. **Day-by-Day Vectorized Execution**:
   - Order arrival queues processed using NumPy's C-accelerated `np.add.at`.
   - Shelf consumption and shortfall detection.
   - Sister branch pooling resolution for critical imports.
   - Phantom ledger divergence tracking.
   - Replenishment orders evaluated against book stock position:
     $$\text{EffectivePosition}_i(t) = \text{BookStock}_i(t) + \text{OnOrder}_i(t)$$
3. **Attribution Decomposition**:
   Four distinct scenarios are evaluated under identical demand seeds:
   - **Scenario 1 (Baseline)**: Legacy unlinked ledger + static min-max + no pooling.
   - **Scenario 2 (Stocking Only)**: M2/M3 dynamic policies + booking forecast + sister pooling, but Legacy unlinked ledger.
   - **Scenario 3 (Tracing Only)**: Gated ledger + scheduled counts, but static min-max policies.
   - **Scenario 4 (Proposed)**: Full twin-engine closed loop (Dynamic Stocking + Gated Traceability).
