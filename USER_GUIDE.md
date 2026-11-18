# User Guide: how to use the Supply Chain Disruption Agent

A plain-language guide for people who use the dashboard day to day. No coding knowledge needed.
(Developers: see [README.md](README.md) for setup, deployment and configuration.)

**Contents:** [What it is](#1-what-this-app-is) · [What the AI does](#2-what-the-ai-does-and-does-not-do) ·
[Sign in](#3-signing-in-for-the-first-time) · [The screens](#4-the-screens) ·
[Full worked example](#5-a-full-example-from-alarm-to-finished) · [Words explained](#6-words-you-will-see-explained) ·
[Roles](#7-who-can-do-what) · [Demo vs real use](#8-demo-versus-real-use) · [Cautions](#9-cautions) ·
[Troubleshooting](#10-if-something-goes-wrong)

---

## 1. What this app is

A watchtower for your supply chain. When something goes wrong (a ship is late, a supplier has a problem, stock is
running low), the app:

1. **Notices** the problem.
2. **Works out the damage:** which products run out, which customers are hit, how much money is at risk.
3. **Tests the risk** by simulating thousands of "what if" futures.
4. **Finds fixes** (expedite a shipment, buy from another supplier, or just wait) and recommends one.
5. **Decides who acts:** small, safe fixes it does by itself. Bigger ones **wait for a person to approve**.
6. **Places the order** once approved, and learns from the result afterwards.

**Your main job is to review the plans it proposes and approve or reject them.**

## 2. What the AI does (and does not do)

The AI is a large language model (DeepSeek). It has a narrow job. The numbers and the final decision stay with
ordinary code and with you.

**Where the AI is used**

| Step | What the AI does |
|---|---|
| Detection | Reads the alert, writes a headline and summary, suggests a severity (it can raise the rules' severity but never lower it), names the likely root cause |
| Impact | Explains the damage in 2 to 3 plain sentences and lists the key business risks |
| Sourcing | Looks at the recovery options and picks the best one, weighing cost, risk, supplier reliability, VIP customers and contract clauses |
| News scanning | Reads a news headline and decides whether it threatens one of your supplier regions |
| Post-mortem | After an order arrives, compares plan with reality and writes lessons learned |

**Where there is no AI**

- The numbers (stock levels, delay effects, costs, quotes) come from ordinary code and your data.
- The risk simulation is statistics (Monte Carlo): it runs 10,000 random "what if" scenarios.
- The rules for "does a human need to approve this?" are plain rules (cost, supplier reliability, AI confidence).
- Creating the purchase order and sending it to your ERP is ordinary code.
- Contract search uses a simple hashing method by default, not a real AI model.

**Why it is built this way:** AI is good at reading messy text and weighing trade-offs, but poor at arithmetic and can
invent things. So code computes the options first, the AI only *chooses from that list* or writes a summary (and its
answer is rejected if it is malformed), rules decide whether a human must approve, and **you** approve anything big.
**The AI advises, code calculates and enforces, a human decides.**

**It works without AI.** If there is no DeepSeek key, the daily budget is used up, or the call fails, each step falls
back to fixed logic (a template summary, and picking the lowest-cost option). The dashboard shows `LLM: fallback`
then, or `LLM: deepseek-chat` when the AI is active. Always check the plan's numbers, not just the summary text.

## 3. Signing in for the first time

1. Start the backend and frontend, then open **http://localhost:3000**.
2. **First time only:** click **Sign in with an API key instead** and paste your admin key. It is the middle part of
   the `name:key:role` entry (for example `root:<this-part>:admin`) in the `API_KEYS` line of `.env`.
3. Click **Users** (top right) and create your own account with the `admin` role, a username and a password of at
   least 10 characters.
4. Click **Sign out**, then sign in with your username and password from now on.

There is no public sign-up: only an admin can create accounts, because this tool approves purchase orders. Keep the
admin API key safe: it is your way back in if everyone is locked out.

## 4. The screens

**Control Tower (home)**

| Tile | Meaning |
|---|---|
| Open disruptions | Problems currently being tracked |
| Awaiting approval | Plans waiting for a person. **The number to watch.** |
| Value at risk | Money you could lose if nothing is done |
| Auto-executed | Small fixes the app did on its own |
| Mitigation spend | Money spent on fixes so far |
| Active risk signals | Outside warnings (weather, news) that may matter |

Below the tiles: the **demo buttons** (see section 8), a **table of disruptions** (severity, status, suggested
purchase order) and a **map** (green = supplier, red = at risk, dashed line = delayed route).

**Other pages:** *Operations* (orders, shipments, purchase orders) · *Suppliers* (each supplier and its reliability) ·
*Account* (change your password) · *Users* (admins only).

**Statuses**

| Status | Meaning |
|---|---|
| analyzing | The app is still working on it |
| awaiting approval | Waiting for a person. That is you. |
| auto-executed | Small and safe, so the app acted on its own |
| approved executed | A person approved it and it was carried out |
| monitoring | Low risk, so the app is only watching |
| rejected | A person said no; nothing was bought |
| resolved | Finished, with the post-mortem written |

## 5. A full example, from alarm to finished

This is a real run of the demo scenario **"Vessel rollover (+10d)"**, with the real numbers it produced. (The run used
the no-AI fallback, so the wording is template text. With the AI key the summary wording would be written by the AI,
but the numbers and options would be identical, because code produces them.)

### Step 1: The alarm
You click **Vessel rollover (+10d)**. That simulates a message from a carrier: shipment SHP-1002 is delayed by
**10 days**. A few seconds later a new row appears by itself:

> **critical** · Shipment Delay: LIC-18650 from Hanoi Assembly Co · awaiting approval · value at risk **$71,083**

Click the row to open the detail page.

### Step 2: What the app found (Impact analysis)
| Item | Value | What it means |
|---|---|---|
| On hand | 8,100 units | What is in the warehouse now |
| Daily demand | 900 units/day | How fast it is used |
| Days of cover | 9.0 days | Stock lasts 9 days |
| Replenishment ETA | day 19 | The delayed shipment now arrives on day 19 |
| Stockout window | day 9 to day 19 | **10 days with nothing on the shelf** |
| Shortfall | 9,000 units | What you cannot supply during the gap |
| Revenue at risk | $43,200 | Orders you would lose or delay |

It also lists the customers hit: **VoltEdge Mobility** and **Initech Power**. These are VIP customers, which matters
later. In words: *"LIC-18650 stocks out in ~9 days and recovers around day 19, losing ~9,000 units; 2 orders
($43,200) at risk."*

### Step 3: How bad could it get (Monte Carlo risk)
The app replays the next 34 days **10,000 times** with random variation in demand and delays:

| Measure | Value | What it means |
|---|---|---|
| Expected loss | $71,083 | The average loss if you do nothing |
| P85 | $97,907 | In 85% of runs the loss was this or less |
| P95 | $118,106 | In 95% of runs the loss was this or less (a bad but plausible case) |
| P(any loss) | about 100% | Doing nothing almost certainly costs you |

### Step 4: The options
The app evaluates the possible fixes. These are the ones it listed (spend in brackets, with the supplier's
reliability score out of 1):

- **Expedite with Hanoi Assembly Co (halve the delay)** ($9,450, reliability 0.76)
- PO 2,700 units from Shenzhen Precision Electronics, China ($6,442, reliability 0.86)
- PO 2,700 units from Chennai Electro Parts, India ($6,076, reliability 0.81)
- Monitor only, no action ($0)

The page ranks them by **total expected cost** = what you spend (premium) + the loss that still remains afterwards.
The winner: **Expedite**, at **$44,374** all-in (premium $9,450 + remaining expected loss $34,924) versus
**$71,083** if you do nothing. The recommendation also shows its confidence (70%), a risk note ("supplier reliability
0.76") and the contract clauses that apply (force majeure sections).

### Step 5: The safety check (Guardrail)
The app does **not** act on its own here. It escalated to a human, with three written reasons:

1. Plan cost $9,450 exceeds the auto-approve limit of $2,000
2. VIP customer orders are affected
3. The disruption is critical

You are now looking at a purple box titled **"Human approval required"**.

### Step 6: You decide
The button area says **"Deciding as <your username>"**.

- Agree with the plan (and you checked the numbers above)? Click **Approve & execute**.
- Not convinced? Click **Reject**. Nothing is bought.

You click **Approve & execute**.

### Step 7: What happens next
- Status changes to **approved executed**.
- A purchase order is created: **EXP-59832C36**, supplier Hanoi Assembly Co, **$9,450**, expected in 14 days.
- It is pushed to your ERP if one is connected. (In this demo it was not, so the order shows "not configured".)
- The log records who approved it and when (your username, never something you can type in).

### Step 8: Closing the loop
When the goods arrive, open **Operations** (or the disruption page) and click **Mark received**, entering how many
days it really took (here, 12 days against 14 expected). The app then writes a **post-mortem** and the disruption moves to
**resolved**. The post-mortem is generated within about 30 seconds by a background check, or straight away if you click
resolve on the disruption. It compares planned and actual results and **updates the supplier's reliability score**, so
future plans trust a supplier more or less based on how it really performed.

*Total time from alarm to approved plan: seconds. The human part is reading the numbers and clicking one button.*

## 6. Words you will see, explained

| Word | Plain meaning |
|---|---|
| Disruption | Anything that threatens supply: a delay, outage, price spike, low stock |
| Days of cover | How many days the current stock lasts |
| Stockout | Running out of stock |
| Value at risk | Money you could lose if nothing is done |
| Expected loss | The average loss across the 10,000 simulated futures |
| P85 / P95 | "In 85% / 95% of the futures, the loss was this much or less": a bad-day measure |
| Premium | Extra money spent to fix the problem (rush fee, higher unit price) |
| Residual loss | Loss that still remains even after the fix |
| Reliability | A supplier's track record, from 0 to 1 (higher is better) |
| PO | Purchase order |
| ERP | The company system that holds orders and inventory (SAP, NetSuite and so on) |
| VIP | An important customer, so late orders are treated more seriously |
| Guardrail | The safety rules that decide whether a person must approve |
| Auto-approve limit | Plans above this cost always need a human (default $2,000) |

## 7. Who can do what

| Role | Can do |
|---|---|
| viewer | Look at everything |
| approver | Also approve and reject plans, mark orders received, resolve, use the demo buttons |
| admin | Also manage users and reset the demo data |

## 8. Demo versus real use

- **Demo (learning):** the buttons under "Inject a disruption" invent problems (East Asia port congestion, vessel
  rollover, supplier lead-time spike, low stock at Dallas, cargo temperature excursion, supplier force majeure).
  Click one and follow section 5.
- **Real use:** problems arrive by themselves from your ERP, suppliers, trackers, sensors, weather and news. The demo
  buttons are hidden in production mode, and your job is just to review and approve.

## 9. Cautions

- **"Reset demo data" deletes data.** If the app is connected to Supabase, that erases real rows in your database.
  Never click it on a real system.
- **Check the numbers, not just the summary.** The AI's wording is helpful, but the figures in the tables are what to
  trust.
- **When unsure, reject.** Anything above the auto-approve limit is the app asking you to check.
- Do not share your password or API key. Admins can disable an account or reset a password under **Users**.

## 10. If something goes wrong

| Problem | What to do |
|---|---|
| "Invalid username or password" | Check spelling and caps. After 8 wrong attempts a username is locked for 5 minutes. An admin can reset it. |
| Locked out completely | Sign in with the admin API key and reset the account under **Users** |
| "Backend unreachable" | The backend is not running. Start it. |
| Nothing appears after clicking a demo button | Wait about 10 seconds, then refresh |
| Badge says `LLM: fallback` | No DeepSeek key was loaded, or the daily budget is used up. The app still works with fixed logic. |
| Badge says `DB: sqlite` / `Bus: memory` | The Supabase or Redis settings in `.env` were not loaded. Check the file is saved and restart the backend. |
