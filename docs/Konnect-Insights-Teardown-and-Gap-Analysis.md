# Konnect Insights — Product Teardown & Gap Analysis for Watch-Tower / VedicReport

**Prepared:** 9 September 2026
**Method:** live walkthrough of a logged-in Konnect Insights trial instance (`cx.konnectinsights.com`, Super Admin), plus their public site, all 17 release notes (Jun 2023 → Jul 2026), official marketplace listings (Salesforce, Zendesk, Freshworks, Genesys, Fivetran), and independent reviews (G2, Capterra).
**Compared against:** the VedicReport report tool + client portal, and the `portal/scraper.py` Collector adapter you call Watch-Tower.

Every claim marked **[TRIAL]** was seen with my own eyes inside the running product. **[DOCS]** = stated by the vendor. **[3P]** = third-party only, treat as softer.

---

## 0. The short answer

You asked three things. Here they are up front.

### Is there anything big we're missing?

**Yes — one thing, and it is the whole product.**

Konnect Insights **finds** content. You **receive** content. That is the entire structural difference, and everything else follows from it.

Your pipeline starts with *"here is a Google Sheet of post URLs someone collected."* Theirs starts with *"here is a Topic — a boolean keyword query — go and discover every public post, comment, review, news article and forum thread on earth that matches it, forever, in near-real-time."* Their Topic engine is not a feature next to reporting; it is the thing that feeds everything else. Share of Voice, sentiment trends, crisis alerts, competitor benchmarking, the unified inbox, the ticket queue — none of it can exist without discovery underneath.

Five more that are genuinely big, in order of how much they hurt:

| # | Gap | Why it's serious |
|---|---|---|
| 1 | **Discovery / listening** | You cannot answer "what is being said about X?" — only "here are the posts I was given." |
| 2 | **Sentiment + classification + severity** | This is the taxonomy layer every downstream analytic hangs off. You have zero. Grok is wired but unused. |
| 3 | **No historical snapshots** | Your daily pull *rewrites* history rather than appending to it. `REPORT_TOOL_ANSWERS.md` says so in its own words. This is a data-model defect, not a missing feature, and it silently corrupts every trend chart you will ever draw. |
| 4 | **No alerting of any kind** | Konnect fires crisis alerts in <60s with spike %, sentiment velocity and reach. You have no email, no Slack, no Telegram — a broken nightly pull is invisible until a human opens `/admin/clients`. |
| 5 | **No engagement loop** | You produce reports. They produce reports *and* let an agent reply, assign, escalate and close within an SLA. The engagement half is where the per-seat revenue lives. |
| 6 | **No platform-API metrics** | OCR of a public screenshot can never see reach, impressions, saves, story exits, taps-forward, or reel watch time. Connecting one Facebook page via Graph API gets you ~60 of those for free. |

### What should we focus on and integrate?

Discovery into Watch-Tower; enrichment (sentiment/classification/severity) as a per-mention layer; an immutable snapshot table; alerting; then AI narrative summaries. Full prioritised plan in **§7 and §8**.

### The platform list

**§4** — every app, channel and platform, split by what the integration actually *does*, verified against the live product rather than the marketing page.

---

## 1. What Konnect Insights actually is

Vendor: **Prudence Analytics & Software Solutions Pvt Ltd** (also filed as CE Analytics), Mumbai. Founder/CEO **Sameer Narkar**. Bootstrapped (~$300K raised total), ~130–150 staff, 500+ customers in 30–35 countries, **~$8.5M ARR mid-2025 tracking to $10–11M in 2026** [3P]. Go-to-market is partner-led — ~68–90 ISV partnerships (Salesforce, Genesys, Webex, chatbot vendors) rather than a direct enterprise sales force. Worth noting: that is how a small team reached enterprise buyers without an enterprise sales org.

Positioning: **"AI-Powered Omni-Channel Customer Experience Management Platform."** Not a listening tool, not a social media manager — a CX suite that happens to contain both.

### Module map [DOCS + TRIAL]

| Module | What it does |
|---|---|
| **Social Listening** | Topic-based boolean monitoring across social, news, blogs, forums, reviews |
| **Omni-Channel Ticketing** | Unified inbox → tickets across 30+ channels, with queues, SLA, escalation |
| **Social Analytics** | Owned-channel + competitor performance, per-platform |
| **Social Publishing** | Calendar, scheduling, approvals, asset library, best-time-to-post |
| **BI Tools & Dashboards** | Self-service dashboard builder, ~500–700 widgets [3P] |
| **Konnect AI+** *(paid add-on)* | Auto-classification, summaries, reply drafting, CSAT prediction, churn risk, Next Best Action |
| **KRC — Konnect Research Cloud** | Natural-language BI. "Ask any business question in plain English." Exposed as a **remote MCP server** you can attach to Claude/ChatGPT |
| **Crisis Management** | Spike/anomaly detection, sentiment velocity, war-room dashboards |
| **Quality Assessment** | Agent QA scorecards and coaching |
| **Survey** | CSAT / NPS, incl. inline-in-email surveys |
| **Command Centre** | Real-time monitoring wall |
| **Industry Benchmarks** | Cross-industry performance comparison |

Adjacent products under the same roof: **KonnectChat AI** (chatbots), **KonnectFSM** (field service), **KonnectDSR** (digital sales rooms).

### Their data model [TRIAL — from the Custom Report builder]

The Custom Report field picker exposes the entire schema — roughly **150 display fields and 95 filter fields** on a single message/ticket. This is the most useful artefact in the whole product for you, because it is a free specification of what a mature mention record looks like. Grouped:

- **Identity:** `MessageId`, `SocialMessageId`, `Post Id`, `Ticket Id`, `Link`, `UserId`, `UserLink`
- **Author:** `UserName`, `Screen Name`, `User Followers`, `UserIsVerified`, `User Gender`, `UserLocation`, `User CreatedAt`, `Commenter Levels`, `Commenter Types`, `Social Reputation score`
- **Content:** `Message`, `Title`, `Media Type`, `Messages Type`, `MessageType`, `Language`, `Mentions`, `Publish Date`
- **Geo:** `Country`, `State`, `City`
- **Enrichment:** `Sentiment`, `Net sentiment score`, `Severity`, `System Severity`, `Classifications`, `Parent Classification`, `Classification Hierarchy`, `Classification Level`, `Classification Sentiment`, `Classified By`, `IsClassificationAutomated`, `Brand Tags`
- **Reviews:** `Star Rating`, `Star Rating Number`, `Only Star Rating`, `Web Review Title`, `App Version`, `App Reviewer - Product Name`, `AlexaRank`, `Website`
- **Google Business:** `Google Location`, `…City`, `…State`, `…Street`, `…Code`, `…Rating`, `…Review Count`
- **Workflow:** ~60 fields — `Assigned To/By/Date/TAT`, `FRT`, `QHT`, `Reply TAT`, `Resolved TAT`, `Closed TAT`, `Work In Progress TAT`, `Response TAT`, `Send Mail TAT`, `Ticket Ageing`, `Is SLA First Reply Violated`, `SLA * Violated Bucket`, `Is Business Hours`, `Escalated By`, `Ticket Escalated To`, `No. of Escalation Tickets`, `ReOpen Date/Comment`, `Queue Assign Status`, `Queuing User`, `Comment Delete`, `Message Hide`
- **CRM sync:** `CRM Integration Id/Status`, `CRM Status Master`, `Publish to CRM duration`, `Send To CRM`

Compare to your `post_metrics`: `likes, comments, shares, views, reach, impressions` + URL. That is the size of the gap, stated numerically.

---

## 2. Verified inside the live trial

These are the findings that matter most, because several of them **contradict the marketing** and reveal where Konnect Insights is genuinely weak.

### 2.1 The Topic (listening query) builder [TRIAL]

Structure is three keyword buckets plus optional filters:

```
TOPIC NAME          (label only, not searched)
CONTAINS            — primary search, any of these  (e.g. citibank, citi bank, citi)
AND CONTAINS        — must also contain one or more of these
DOES NOT CONTAIN    — exclude
```

Then:
- **MEDIA PREFERENCE** — pick which sources to crawl
- **REGIONAL** — Country include/exclude (~240 countries), Language include/exclude (~180 languages)
- **EXCLUSIONS** — Website exclusions, X exclusions, Facebook exclusions, YouTube exclusions
- **MORE SETTINGS** — Include Retweets (y/n), Include Replies (y/n)
- **Activation** — pause/resume a Topic; *"During the time it is paused, data will not be accumulated"*

> **This is weaker than it looks, and it is your opening.** It is not a real boolean grammar — there is no nesting, no parentheses, no OR-groups inside an AND, no proximity operators (`NEAR/5`), no wildcards, no phrase-vs-token control, no field-scoped terms (`author:`, `site:`, `title:`). It is three flat lists ANDed together. G2 reviewers complain about exactly this: *"query builder not dynamic enough."* A modern semantic/vector retrieval layer would beat it outright, and would beat Brandwatch and Sprinklr too, because their architectures are also built on boolean indices.

### 2.2 Listening sources — the real list is 12, not "50+" [TRIAL]

The Media Preference picker inside a Topic contains **exactly twelve** options:

```
News · Blogs · Forums · Consumer Forums · Web Reviews · Other - Web · Web Comments
Twitter Public Tweets · Facebook Public Posts · YouTube · Instagram · TikTok Public Posts
```

**LinkedIn, Reddit and Quora are not selectable listening sources** — despite all three appearing in marketing. Reddit and Quora presumably fall inside the generic *Forums* / *Consumer Forums* crawlers, and LinkedIn keyword data is *"available upon request"* per their Nov 2023 release note. So the honest count is **5 social sources + 7 open-web crawler buckets**. Every other "channel" in their 30+/50+ figures is an **owned account you connect with credentials**, not something they discover for you.

That reframes the whole competitive picture: their *discovery* surface is narrower than yours would need to be, and it is achievable.

### 2.3 No historical backfill [TRIAL — this is their biggest weakness]

Quick Search, verbatim from the UI:

> *"Quick Search feature helps you to test your queries before you make them live. **You can get sample data of 7 days using Quick Search.** From Quick Search you can make the topics live once you are happy with the query and results."*

Seven days of sample data to test a query, and a Topic accumulates **only from the moment you activate it**. Combined with the repeated G2/Capterra complaint of *"only ~1 month of historical data,"* this means **Konnect Insights cannot answer a question about the past.** Brandwatch and Talkwalker sell multi-year archives; that is their moat and Konnect has no answer to it. If you ever build a proper immutable snapshot store (§7.2), you will be ahead of Konnect on the one axis their own customers complain about.

### 2.4 Indian-language support is real and specific [TRIAL]

The language filter includes, by name: **Hindi, Marathi, Bengali, Gujarati, Tamil, Kannada, Telugu, Malayalam, Punjabi, Assamese, Oriya, Nepali, Sindhi, Kashmiri, Sanskrit, Urdu**, and — importantly — **`Latinized Hindi`** as a distinct language, i.e. **Hinglish is a first-class detected language.** There is also `Undefined Language`.

Do not underestimate this. Romanised Hindi mixed with English defeats both language detection and monolingual sentiment models, and the failure mode is *silent* — English sentiment on Hinglish returns confidently wrong output. Konnect has explicitly modelled it. For an India-focused product this is table stakes, not a differentiator.

### 2.5 Admin configuration surface [TRIAL — `Settings → Admin`]

This is the operational depth you would have to match to sell into a CX buyer:

**Ticket Operations (14):** Classifications (hierarchical buckets + auto-classify + auto-assign rules) · Severity · SLA · TAT Settings (business hours, weekends, holidays) · Ticket Settings · Ticket Priorities in Queuing (keyword-based queue jumping) · Ticketing View Master · Queued Messages Configuration · Email Notification · Draft Messages Template · **Sentiment Customization** *("Override the NLP logic by setting up keywords to mark the sentiments as Positive, Negative or Neutral")* · User Custom Fields · Email · Custom User Status

**Automations & Quick Actions:** Automation (rules on classification, sentiment, time of day, influence…) · CSAT Settings · Quick Action (bulk ops under pre-defined conditions)

**My Task Settings:** CRM Settings (escalation matrix) · CRM Draft Messages Template

**Email Escalations:** Escalate Mail Settings (auto follow-up after N duration)

**Integrations:** **External APIs** (agents call your systems from inside a ticket) · **Webhooks** (outbound, retries up to 10 attempts before deactivation)

**Customer Experience:** Survey

That **Sentiment Customization** entry is worth copying verbatim into your design: a keyword override on top of the model. It is how you ship sentiment before your model is good, and how you let a client fix "cancelled my order" being scored neutral without a retrain.

### 2.6 Report catalogue [TRIAL — `Monitor → Reports`]

```
SOCIAL LISTENING       Share of Voice · Sentiment Analysis · Media Type Analysis
                       Twitter Report · Instagram Report · Classifications
COMMUNITY ENGAGEMENT   All Task Report · My Task Report · CSAT Report
                       Ticketing Report · Queuing Report · My Dashboard
CALLS ANALYTICS        (module-gated on this trial)
```

Plus **Download** (bulk export with saved header templates), **One-Click Report**, **Custom Report** (the field-picker builder), and **Dashboard** — *"Dynamic Dashboards… build your own dashboard with charts from Monitor and Social Analytics and from Custom Widgets modules."*

### 2.7 Social Analytics [TRIAL]

Per-platform analytics for **X, Facebook, Instagram, YouTube, LinkedIn, Google Analytics, Google Ads, Facebook Ads, Google My Business, TikTok, Threads**, plus a **Comparison** section (X Comparison, Facebook Comparison, Instagram Comparison, …) for competitor benchmarking, plus **Streams**.

The **Glossary** page enumerates every metric they pull. A sample of what is only obtainable via official APIs — and therefore permanently invisible to your screenshot/OCR pipeline:

> Tweet Impressions · Quotes · Link Clicks · User Profile Clicks · Complete Views · Page/Post Reach split into Organic / Paid / Viral / Non-Viral · Negative Feedback by Type · Likes Sources / Unlike Sources · People Talking By Country / By City · Post Storytellers · Story Impressions / Reach / Exits / Taps Forward / Taps Back / Story Completion Rate · Saves · Follows · Profile Visits · Profile Activity · Reels Watch Time / Average Watch Time / Plays / Initial Plays / Replays / Total Plays · Views 10s / 30s / 60s with Organic / Paid / Unique / Autoplay / Clicked-To-Play breakdowns · Subscribers Gained / Lost · Watch Time · Annotation & Card Impressions/Clicks · Consumption Rate · Directions / Phone Calls / Email Contacts / Website Clicks / Text Message Clicks (Instagram Business profile taps)

Your `metrics/README.md` is honest about this: *"Insights figures (impressions and reach as the platform's own dashboard reports them) are not on a public post and are not read."* Correct — and unfixable by better OCR. The only route is OAuth on an owned account.

### 2.8 Competitor tracking [TRIAL]

"Add a new Comparison Profile" accepts: **X handle · Facebook page URL · Instagram profile URL · TikTok username · Google Business profile name · Google Play Store app name · Trustpilot page URL · iOS App (search & add)**. Note the gating: *"Connect your X account before adding competitor's profile"* — you must own the corresponding channel first.

### 2.9 Trial limits observed

The Publish module returned **"You do not have access to this module"** on this trial, so publishing is plan- or module-gated. Everything else was reachable.

---

## 3. Pricing — your commercial anchor

Konnect publishes list prices, which is unusual and useful [DOCS]:

| Plan | Price (billed yearly) | Adds |
|---|---|---|
| **Starter** | **$39 / user / month** | Ticketing, engagement, listening, publishing, dashboards, workflow automation, standard integrations |
| **Professional** | **$79 / user / month** | Advanced automation, enhanced analytics, multi-team, expanded integrations, advanced reporting |
| **Advanced** | **$119 / user / month** | Enterprise security, custom integrations, dedicated support, governance, multi-brand |

**Consumption charges on all plans:** **$0.03 per social mention per month** · **$55 per social profile per month**. Add-ons priced on request: Konnect AI+, AI Quality Assessment, Premier 24/7 support, Professional Services.

**Typical deal:** ACV **$22–25K**, entry deals $10–12K, largest accounts >$100K [3P, Latka].

The model is worth studying: **per-seat + per-mention + per-connected-profile**. The per-mention line is what scales — 1M mentions/month is $30,000/month at list, which is why real deals get negotiated. If you productise Watch-Tower, this is a proven three-axis pricing shape for this market.

**Free trial:** 14 days, no credit card. Signup asks name, business email, password, phone, timezone, Brand-or-Agency.

> A note on the trial: you already had a signed-in instance open, so I explored that. If you want additional trials set up, you'll need to do the signup yourself — I can't create accounts or enter passwords on your behalf. I can drive everything after you're logged in.

---

## 4. Every platform, app and channel — the complete list

Split by what the integration actually **does**, because the marketing collapses four very different things into one "30+ channels" number.

### 4.1 Listening — discovered without credentials [TRIAL, authoritative]

| Source | Notes |
|---|---|
| Twitter / X Public Tweets | Konnect is an **Official X Enterprise Partner**; claims unsampled real-time enterprise-tier access |
| Facebook Public Posts | |
| Instagram | |
| YouTube | Public videos by keyword, shorts, channel comments, community pages |
| TikTok Public Posts | Native since ~Jun 2025; older reviews said TikTok was missing — verify depth |
| News | Proprietary crawlers + APIs |
| Blogs | |
| Forums | Reddit / Quora presumably land here |
| Consumer Forums | ConsumerComplaints, MouthShut cited [DOCS] |
| Web Reviews | |
| Web Comments | |
| Other – Web | Catch-all |

**Not listenable:** LinkedIn (keyword data "on request" only), Snapchat, Threads-as-listening, Telegram public channels, Twitch, Tumblr, VK, Weibo, Mastodon, Bluesky, WeChat.

### 4.2 Owned accounts — connect with credentials [TRIAL, from the Add Profile modal]

**Social media (20):** Facebook · X (Twitter) · Instagram via Facebook · Threads · YouTube · LinkedIn · Google Play · Apple Connect · Google Business · TikTok · TikTok (AyrShare) · Telegram · Slack · Line Chat · Google Ads Account · Google Analytics (GA4) · Facebook Ads Account · Discourse · Pinterest · Discord

**Calls / CTI (6):** Exotel · Knowlarity · AirCall · MyOperator · Genesys · Ameyo

**Email (4):** Gmail · Microsoft 365 · Zoho Mail · Custom (IMAP/SMTP)

**WhatsApp (6):** Engati · Yellow · Gupshup · Wati · Limechat · **Meta Business** (direct Cloud API)

**Live Chat (2):** Engati · Yellow

**BYOC — Bring Your Own Channel (1):** generic API ingestion for any third-party WhatsApp / email / SMS / form / QR-feedback source. This is the escape hatch behind the "100% coverage" claim.

### 4.3 Reviews & ratings

| Platform | Capability | Confidence |
|---|---|---|
| **Google Business Profile** | Reviews, respond, location management, Q&A, posts/offers, 8+ analytics charts, location-grouped tickets | [TRIAL + DOCS] — deepest non-social integration |
| **Google Play Store** | App reviews, reply, ticketing, competitor app tracking | [TRIAL] |
| **Apple App Store** | iOS reviews, reply; needs Issuer ID + Key ID + Admin Private Key | [TRIAL] |
| **Trustpilot** | Reviews, respond, competitor page | [TRIAL] |
| **Amazon** | Product reviews | [DOCS] |
| **Flipkart** | E-commerce reviews | [DOCS] |
| **Zomato, TripAdvisor** | Reviews | [DOCS] |
| **G2** | Listed under review platforms | [DOCS, ambiguous] |
| "15+ review platforms" | Unnamed bucket | [DOCS] |

**Notably absent by name:** Swiggy, Yelp, MouthShut, JustDial, AmbitionBox, Glassdoor, Booking.com. For an India-headquartered vendor that is a real hole — and a place you could differentiate.

### 4.4 Community & forums

Quora · Glassdoor · Indeed · Reddit · ConsumerComplaints · MouthShut · **Discourse** (admin API key, ~30 min capture delay) · **Discord** (bot token, choose servers/channels, 5–10 min delay).

### 4.5 CRM, helpdesk & enterprise

Salesforce Service Cloud (AppExchange app — bidirectional ticket/contact sync, reply to social from Salesforce, field mapping, rules on sentiment/keywords/followers) · Zendesk (Marketplace app) · Freshdesk / Freshworks (Marketplace app) · Microsoft Dynamics 365 · Zoho CRM · HubSpot · Shopify · SurveySensum · **AirSewa** (Indian aviation grievance portal) · Yellow.ai · Engati · Gupshup.

**Absent:** SAP, ServiceNow, Oracle CX.

### 4.6 Collaboration, alerting, storage, productivity

Slack (real-time alerts) · Microsoft Teams · Google Chat · Telegram (alerts, 10-min minimum interval) · Email + SMS alerts · Zoom · Google Meet · Webex · Jira · Trello · Asana · Google Calendar · Calendly · Google Sheets · Excel · Zoho Sheets · Google Drive · OneDrive · Dropbox · AWS S3 · Mailchimp · Outlook · Typeform · Google Forms · Bitly · PayPal · Razorpay · **Zapier / airSlate / Appy Pie** (7 triggers + Create Ticket action → 9,000+ apps).

### 4.7 BI & data

**Google Analytics GA4** (native — Active Users by Country/City/Language/Minute/Device/Demographics/Top Channel) · **Facebook Ads Manager** · **Google Ads** · **Fivetran** official connector (tables: `MESSAGE_CLUSTER`, `MESSAGE_PROFILE`, `MESSAGE_TOPIC` + `CLASSIFICATION`, `SEVERITY`, `COMMENTER_TYPE`, `TOPIC`, …) · **Open REST API** at `developer.konnectinsights.com` (cURL/Java/Node/PHP/Python/.NET samples) · **Webhooks** · **KRC as a remote MCP server** attachable to Claude or ChatGPT.

**No** Power BI, Tableau, Looker or Qlik connector.

### 4.8 Compliance & hosting [DOCS]

Certifications claimed: GDPR · SOC 2 · ISO 27001:2022 · ISO 42001 · CCPA · HIPAA · **DPDPA** · NDPA.
Hosting regions: India · UAE (AWS) · UK (AWS) · Germany (AWS) · Saudi Arabia (Oracle Cloud).
Security controls seen/documented: IP whitelisting (Super Admin only), 2FA via SMS, PII encryption on customer phone/email with access logging, field-level encryption on Additional Info, outbound email domain restrictions.
UI languages: English, Arabic (RTL), Spanish, French, Portuguese.

---

## 5. Functional deep-dive — the modules worth copying

### 5.1 Omni-channel ticketing (their deepest module)

**Views:** All Messages (raw inbox) → capture as ticket → **Queued Tickets** → **My Tasks** (agent) / **All Tasks** (supervisor). **One Ticket View** splits Public (mentions, comments) from Private (DMs). **Conversational View** unifies public + private from the same customer with journey, notes, analytics; March 2026 added left/right chat alignment.

**Routing — five assignment types** [DOCS, Mar 2026]: Priority · Equal · Round Robin · Round Robin One-by-One · Round Robin (availability-based). Plus Manual Queue Assignment, Sticky Assignment, status-based routing, user groups by time zone, **Smart Queue Cleanup** (auto-reassign unattended tickets), **Queue Timer** (green for 10 min then red), admin can pause an agent's queue.

**SLA/TAT:** FRT, ERT, RT, Reply TAT, Close TAT, tracked to the second. Configurable per channel / priority / segment with business hours and holidays. **SLA violation detection at 1-minute intervals** (was 5). **Predictive alerts at 50 / 70 / 90 % of the SLA window**, routed to named people. SLA Priority Filtering auto-floats at-risk tickets. Auto-escalate via email with agent signature and domain restrictions.

**Agent tooling:** Draft templates (bulk-uploadable, with dynamic placeholders) · agent signatures · internal notes with @-mentions · reminders · **collision detection** · **parent–child tickets** · merge tickets · bulk quick actions · audio-to-text in the reply box · GenAI spelling/grammar/translation (90+ languages) · Bitly · Knowledge Base · in-inbox moderation (hide/delete FB & IG comments, block X/FB DM users, hold YouTube comments).

**Customer 360:** identity resolved across channels, user journey with 30/60/180-day filters, conversation history export, PII encryption with access logging, Salesforce/Dynamics enrichment (account tier, LTV, renewal date, open opportunities) with resolution write-back.

### 5.2 Konnect AI+ (the paid AI layer)

Auto-classification & smart tagging · sentiment, emotion, intent and severity detection · **instant conversation summaries** ("long threads become a one-line brief") · real-time reply suggestions & auto-drafted responses · tone/quality guidance · **CSAT prediction** · **churn risk** · **Next Best Action** with one-click execution · smart routing & prioritisation · emerging trend alerts · **Knowledge Grounding** (RAG over your docs/SOPs/past resolutions, **with source attribution**) · **Customizable Signals** (define business-specific fields like upsell potential, escalation likelihood, VIP status) · natural-language Q&A · **per-chart AI 120-word insights** · GenAI content and image generation.

That **per-chart 120-word insight** is the cheapest high-value thing on this list and you can ship it this month — Grok is already wired in `webapp/config.py`.

### 5.3 Crisis Management

Alerts in **under 60 seconds** with source, sentiment and reach · **weak signal detection** (rising negative sentiment before it trends) · mention spike percentages ("↑ 840% Mention spike") · **sentiment velocity** (accelerating or plateauing) · live risk score by topic/region/channel · auto-escalation by severity/channel/keyword · amplifier and influencer identification with loyalty history and outreach priority.

Note the design idea: **velocity, not volume.** Rate of acceleration is what predicts a crisis; absolute count does not.

### 5.4 Publishing [TRIAL nav + DOCS]

Nav: All Posts · Drafts · Content Tags · **Assets Library** · **Bulk Scheduling** · **Location Management**.
Calendar view · scheduling for posts/stories/reels · bulk scheduling via Excel · **multi-approver workflow** (all designated reviewers must approve) · asset library approval workflow · DAM with 1 GB free storage connecting to S3 / OneDrive / Google Drive · per-network device preview · **AI best-time-to-post** · GenAI captions, images and hashtags · failed-post edit & reschedule · image editor with preset aspect ratios · feed targeting (Facebook by age/location/language; LinkedIn by seniority, company size, industry, function, education) · deep per-network controls (IG collaborators & copyright verification, LinkedIn SRT captions & document posts & event posts, X polls & reply restrictions & 25k-char Premium posts, Threads polls & ghost posts, GMB offers with voucher codes).

### 5.5 Surveys / CSAT / NPS

CSAT Report & NPS Score · CSAT automation driven by custom/additional info fields · **inline CSAT embedded in the email body** · social-profile CSAT triggered via email when contact email is known · custom background and thank-you pages · CSAT prediction in AI+ · SurveySensum integration.

---

## 6. Head-to-head: Konnect Insights vs. VedicReport + Watch-Tower

| Capability | Konnect Insights | You today | Gap |
|---|---|---|---|
| **Discovery (find posts by keyword)** | Topic engine, 12 sources, boolean, geo/lang filters, near-real-time | None — URLs must be supplied | 🔴 **Critical** |
| Post identity | `SocialMessageId` + `Post Id` | None; dedupe on normalised URL only | 🔴 Critical |
| Historical snapshots | Append-only mention store *(but only ~1 month deep)* | **Rewrites** history on each pull | 🔴 Critical |
| Sentiment | 3-class + net score + keyword override + emotion/intent | None | 🔴 Critical |
| Classification / tagging | Hierarchical, bulk-uploadable, auto-classify rules | None | 🔴 Critical |
| Severity | Configurable per media type, drives automation | None | 🔴 Critical |
| Alerting | <60s crisis, spike %, velocity, SLA 50/70/90%, email/Slack/Telegram | **None at all** | 🔴 Critical |
| Ticketing / inbox | Full — queues, SLA, escalation, collision, parent-child | None | 🟠 Strategic choice |
| Owned-channel API metrics | ~200 metrics via official APIs | Public counts only, via OCR | 🟠 High |
| Author/audience data | Followers, verified, gender, location, reputation score | None | 🟠 High |
| Share of Voice / competitors | Yes, 8 competitor profile types | None | 🟠 High |
| Geography | Country/State/City on every mention | None | 🟠 High |
| Language detection | ~180 incl. **Latinized Hindi** | None | 🟠 High |
| Review platforms | GBP, Play, App Store, Trustpilot, Amazon, Flipkart… | None | 🟠 High |
| Publishing | Full suite | None | 🟡 Optional |
| Custom report builder | 150 display + 95 filter fields, save as template | 11 fixed styles | 🟡 Medium |
| Dashboards | Self-service builder, 500+ widgets | 4 fixed portal views | 🟡 Medium |
| Scheduled report delivery | Alerts + scheduled downloads | Manual | 🟡 Medium |
| Open API + webhooks | Yes, both, with retries | `/v1` read API; **no inbound ingest, no webhooks** | 🟠 High |
| Multi-tenancy | Groups, roles, IP allowlist, 2FA, PII encryption | Staff/client two-tier, client isolation ✅ | 🟢 **Comparable** |
| **Branded report output** | PDF/Excel; PPT not documented | **PDF + DOCX + PPTX with movable objects, 11 styles, Canva design kit** | 🟢 **You win** |
| **Client delivery portal** | Shareable dashboard links | Dedicated per-client portal with lag_days, category map, branding | 🟢 **You win** |
| **Screenshot fidelity** | Not a feature | Full logged-in post captures, reply-with-parent, hi-res | 🟢 **You win, uniquely** |

### Where you are genuinely ahead

Do not lose this in the gap analysis. Three things you do that Konnect Insights does **not**:

1. **Pixel-accurate post screenshots**, including a reply captured together with its parent, at high resolution, from a logged-in session. Konnect has no equivalent. For PR, agency and legal-evidence reporting this is the deliverable.
2. **Designed, editable PPTX output** where every screenshot, value, button and summary table is a separate movable object — plus a Canva design kit with slot guides and embedded fonts. Konnect's export story is Excel and is a documented customer complaint.
3. **A real client-delivery portal** with per-client branding, category mapping and a deliberate publication lag. Konnect shares dashboard links; you ship a product to the client.

Your wedge is **"the report and the client experience,"** not "a cheaper Konnect Insights."

---

## 7. What to build into Watch-Tower (the Collector)

Ordered by leverage. Items 1–4 are the ones that change what the product *is*.

### 7.1 A Topic model — and make it better than theirs

Minimum parity:

```
topic:
  id, project_id, name, active
  contains:        [str]     # OR within
  and_contains:    [str]     # OR within, ANDed against contains
  not_contains:    [str]
  sources:         [enum]    # x, youtube, news, blogs, forums, reviews, gbp, appstore, playstore
  countries:       {include:[], exclude:[]}
  languages:       {include:[], exclude:[]}
  exclude_domains: [str]
  exclude_authors: {x:[], youtube:[], facebook:[]}
  include_retweets: bool
  include_replies:  bool
```

Then beat them, cheaply, on the two axes their own reviewers complain about:

- **Real boolean:** nested groups, `OR` inside `AND`, `NEAR/n` proximity, `"exact phrase"`, wildcards, and field scoping (`author:`, `site:`, `title:`).
- **Semantic recall on top of boolean.** Embed every mention on ingest; run hybrid retrieval (BM25 + vector). Then a Topic can say *"complaints about slow delivery"* and catch every phrasing, misspelling and Hinglish variant without anyone hand-writing a keyword list. **This is your single biggest structural advantage** — incumbents including Konnect, Brandwatch and Sprinklr are all built on boolean indices and cannot retrofit this easily.

Ship a **Quick Search** equivalent too (test a query against a 7-day sample before going live). It is a small feature that makes query-building tolerable, and its absence would be felt immediately.

### 7.2 Fix the data model before anything else

This is the highest-priority item in the whole document, because everything downstream inherits the defect.

Your `REPORT_TOOL_ANSWERS.md` states it plainly: *"a daily pull today does not build history — it **rewrites** it. Yesterday's 'likes on 8 Sep' becomes today's live like count."* Every trend chart in the portal is therefore wrong, and quietly so.

Three changes:

1. **Stable post identity.** Add `platform` + `native_post_id` as the primary key; keep normalised URL only as a secondary index. You have no post id column at all today.
2. **Split the tables.** `posts` (immutable content — author, text, media, posted_at, language, geo) and `post_metric_snapshots` (`post_id, captured_at, likes, comments, shares, views, reach, impressions, quotes, bookmarks`), append-only. Growth becomes a query over snapshots instead of a browser-side subtraction of two overwritten rows.
3. **Honour `status`.** You currently ignore it, so *"removed/unavailable posts would render as live."* Add `status` + `last_seen_at` and exclude dead posts from live views without deleting their history.

While you are in there, add the columns the adapter is already missing: `project_id`, `author_avatar`, `author_followers`, `group`, `day`, `quote_count`, `bookmark_count`, `lang`, `tweet_id`.

### 7.3 Fix the adapter's transport

From your own gap doc: *"With `limit=500` everything past the first page is silently dropped."* Add cursor-based paging, a `since`/`until` window, retry with backoff, and a per-source quota manager. Silent truncation is worse than an error.

### 7.4 An enrichment pipeline

Run on ingest, store on the mention, never recompute at read time:

| Field | How | Notes |
|---|---|---|
| `language` | fastText / CLD3 + a **Latinized-Hindi (Hinglish) detector** | Copy Konnect's decision to treat Hinglish as its own language |
| `sentiment` + `sentiment_score` | LLM few-shot with Indian-context examples, **plus a client-configurable keyword override table** | The override table is how you ship before the model is good. Steal `Sentiment Customization` outright |
| `classification` | Hierarchical parent/child, Excel bulk upload, LLM auto-assign with confidence | |
| `severity` | Rules by media type + follower count + sentiment | Drives alerting |
| `is_spam` / `is_irrelevant` | Classifier + per-topic exclusions | |
| `author_*` | followers, verified, location, avatar | Enables influencer identification |
| `geo` | country / state / city | |
| `embedding` | Vector, on every mention | Powers §7.1 semantic search and §8.4 clustering |

Costs are trivial at your volumes — a single cheap LLM call can return language, sentiment, classification and severity together.

### 7.5 Expand sources — cheapest first

| Source | Route | Cost | Priority |
|---|---|---|---|
| **X / Twitter** | Official API, now **pay-per-use** since 6 Feb 2026 | **$0.005** per post read; **$0.001** for your own posts; 3M reads/cycle cap. ~$500/mo at 100K mentions | 🥇 Do first — legitimate, cheap, and it retires your ~320-captures/day account ceiling |
| **YouTube** | Data API v3 | Free at 10K units/day — but `search.list` costs **100 units**, so 100 searches/day. Fine for tracking known channels | 🥇 |
| **Google Business Profile** | Official API | **Free**, but zero starting quota — access request takes days-to-weeks. Start it now | 🥇 Highest India ROI |
| **Play Store / App Store reviews** | Official APIs | Free/cheap | 🥇 Often more complaint volume than social for Indian consumer brands |
| **News / blogs / forums** | RSS + open-web crawl, or a news API | Low | 🥈 |
| **Trustpilot** | API / crawl | Low | 🥈 |
| **Instagram / Facebook (owned)** | Graph API on a connected page | Free | 🥈 Unlocks ~60 metrics OCR can never see |
| **Reddit** | Official commercial API | **$12,000/month floor** (~50M calls min) | 🥉 Defer — the cost cliff is real |
| **Instagram / Facebook public** | ❌ Closed. CrowdTangle shut 14 Aug 2024; Content Library is academic/non-profit only | — | Not viable |
| **LinkedIn** | ❌ Partner-gated, no public listening API | — | Not viable |
| **TikTok** | ❌ Research API is non-commercial, and India is not eligible | — | Not viable |

**Realistic total for a credible listening product: ~$500–2,000/month** covering X + YouTube + GBP + app stores + open web.

**Legal note, briefly.** Two cases define the ground. *hiQ v. LinkedIn* — scraping public data likely doesn't violate the CFAA, **but** hiQ still lost on breach of contract (they used fake accounts) and settled Dec 2022 for $500K plus destruction of all scraped data and code. *Meta v. Bright Data* (Jan 2024, Judge Chen) — summary judgment **for** Bright Data: terms of service bind only **logged-in** users. The practical rule: **logged-out collection of genuinely public pages is defensible; anything touching an account is a straightforward contract claim.** Your X capture pipeline logs in with a shared account. Moving X to the official API removes that exposure and the rate ceiling in one step.

### 7.6 Push, don't poll

Add outbound **webhooks** with retry (Konnect deactivates after 10 failed attempts — a sane default), and an **inbound ingest endpoint** so Watch-Tower can push to the portal instead of the portal pulling hourly. Your gap doc notes there is no inbound endpoint at all today.

---

## 8. What to build into the report tool / portal

### 8.1 Alerting — build this first, it is a weekend

You have literally none, and it is the cheapest credibility win available.

- **Operational:** nightly pull failed / returned zero rows / source unreachable → email + Telegram. Today a broken pull is silent until someone opens `/admin/clients`.
- **Signal:** mention spike (% vs trailing 7-day mean), negative-sentiment spike, **sentiment velocity** (is it accelerating?), a post crossing an engagement threshold, a new 1-star review.
- **Delivery:** email, Slack, Telegram. You already run a Telegram bot — the transport exists.

### 8.2 Surface the enrichment

Once §7.4 lands, add sentiment / classification / severity / language / geo as columns, filters and charts in the portal, and add the two listening reports that matter: **Share of Voice** and **Sentiment Analysis over time**. Also surface `reach` and `impressions`, which you currently store but never display.

### 8.3 A custom report builder

Copy the shape of their Custom Report screen: a **Display Fields** picker, a **Filter Fields** picker, Table-or-Chart output, save as a reusable template, send to dashboard, schedule by email. Your 11 fixed styles are beautiful but rigid; a field picker turns every ad-hoc client request from a code change into a click. Their ~150 display fields (§1) are a ready-made spec.

### 8.4 AI narratives — your fastest visible win

Grok is already wired in `webapp/config.py` and `metrics/shot_metrics.py` and, by your own README, *"has not been exercised against a live key."* Turn it on and ship, in order:

1. **Per-chart 120-word insight** — exactly what Konnect does. One prompt, immediate perceived value.
2. **Weekly client brief** — "what changed, why, and what to do," auto-written from the snapshot table. **This is the single highest-leverage feature for a small team**, because it converts a data product into a decision product, and it is where you can be better than Konnect rather than merely equal.
3. **Natural-language Q&A over the mention store** — their KRC equivalent. Once mentions are embedded (§7.4) this is mostly retrieval plumbing.
4. **Narrative clustering** — group mentions into evolving storylines with momentum scores, rather than counting keywords. Under-served across the whole market.

### 8.5 The strategic fork: do you want the engagement half?

Ticketing is the biggest single block of work in Konnect Insights — five routing algorithms, SLA to the second, escalation matrices, collision detection, parent-child tickets, agent QA. It is also where the $39–119/seat/month lives.

Two honest options:

- **Don't build it.** Stay a listening + reporting + client-delivery product, and integrate outward — push tickets into Freshdesk or Zendesk via webhook. Faster, defensible, plays to the strengths in §6.
- **Build a thin slice.** Status (`fresh/open/wip/resolved/closed`), assignee, internal note, a single SLA timer, reply-from-inbox for X and Instagram only. Enough for an ORM team, ~10% of the work.

I'd take the thin slice, and only after §7.2 and §8.1 are done.

### 8.6 India specifics to design in now

- **Hinglish** as a first-class language, not noisy English. Benchmark modern LLM few-shot against fine-tuned MuRIL/IndicBERT on your own labelled sample — few-shot is often competitive now at far lower engineering cost. AI4Bharat's IndicNLP catalog and the GLUECoS benchmark are the reference points.
- **Indian review surfaces Konnect does not name:** Swiggy, MouthShut, JustDial, AmbitionBox, Yelp. None publish a general review API, so the acquisition is dirty — but that is precisely why it is differentiating.
- **WhatsApp**, if you go there: Meta India rates are Marketing ₹0.785, Utility ₹0.115, Authentication ₹0.115, **Service free**. BSPs add ₹0.20–0.85/message plus ₹999–₹2,00,000/month platform fees. Direct Meta Cloud API costs ₹1.5–5L in development and breaks even against a BSP at roughly **50,000 messages/month**.
- **DPDP Act 2023 + DPDP Rules 2025.** Rules notified 13 Nov 2025. Consent Manager obligations commence **13 Nov 2026**; the core substantive obligations commence **13 May 2027**. Penalties up to **₹250 crore per instance**, and no grace period is expected. Scraped social data containing identifiable individuals **is personal data** — author handles, avatars, bios, location. Design for it now: pseudonymise author identity, set retention windows, de-identify old mentions rather than deleting them (buyers want 24 months of history; DPDP wants minimisation — de-identification resolves the tension), and build the breach-notification path before you need it. Retrofitting an architecture that stores raw personal data everywhere is far more expensive than designing for this from the start.

---

## 9. Suggested sequence

**Phase 0 — before anything else (1–2 weeks).** Fix the data model: stable post identity, `post_metric_snapshots` append-only table, honour `status`, add the missing columns. Nothing else is worth building on top of a store that rewrites its own history.

**Phase 1 — credibility (2–4 weeks).** Alerting (operational + spike). Adapter paging and retries. Turn on Grok for per-chart insights. Start the Google Business Profile API access request today — it takes weeks.

**Phase 2 — become a listening tool (6–10 weeks).** Topic model + query builder + Quick Search preview. X official API as the first collector. Enrichment pipeline (language incl. Hinglish, sentiment with keyword override, classification, severity, embeddings). Surface it all in the portal. Share of Voice + Sentiment reports.

**Phase 3 — differentiate (ongoing).** Semantic/vector retrieval over mentions. AI weekly narrative briefs. Custom report builder. YouTube + GBP + app-store review collectors. Competitor comparison profiles.

**Phase 4 — optional.** Thin-slice ticketing, owned-channel Graph API metrics, publishing.

---

## 10. What to test next in the trial

You have a live instance. Things worth checking that I could not, and that would sharpen this document:

1. **Connect one real X handle and one Facebook page** — then compare the metrics Konnect returns against what your OCR pipeline reads for the same posts. That is a direct measurement of the §2.7 gap, in your own numbers.
2. **Create a Topic on a brand you already report on**, let it run a week, and compare its recall against the link list your team assembles by hand. That is the discovery gap, quantified — and the single most persuasive number for any internal roadmap argument.
3. **Run a Custom Report** and export it. Compare the output quality to your PPTX. I expect you win decisively; confirm it.
4. **Check actual sentiment accuracy on Hinglish** by seeding a Topic with a known code-mixed brand conversation.
5. **Look at `app.konnectinsights.com/apidetails`** for the API credentials screen and the KRC MCP server URL — the MCP endpoint in particular is worth understanding, since it is how they expose their data to Claude.
6. **Ask their sales team directly** about historical data depth and mention volume caps. Neither is published, and §2.3 suggests the answer is unflattering.

---

## Sources

**Live product** (trial instance, 9 Sep 2026): `cx.konnectinsights.com` — `/Settings#admin`, `/Settings#Omni-Channel`, `/Topics`, `/QuickSearch`, `/CustomReports`, `/Glossary`, `/summary`, `/dashboard`, `/publish/scheduledposts`, Reports nav.

**Vendor:** [Homepage](https://konnectinsights.com/) · [Social Listening](https://konnectinsights.com/social-listening/) · [Omni-Channel Ticketing](https://konnectinsights.com/omni-channel-ticketing/) · [Social Analytics](https://konnectinsights.com/social-analytics/) · [Publishing](https://konnectinsights.com/social-media-publishing/) · [BI & Dashboards](https://konnectinsights.com/dashboards-and-bi-tools/) · [Konnect AI+](https://konnectinsights.com/konnect-ai/) · [KRC](https://konnectinsights.com/krc/) · [Crisis Management](https://konnectinsights.com/crisis-management/) · [Channels](https://konnectinsights.com/channels/) · [Pricing](https://konnectinsights.com/pricing/) · [X Enterprise Partner](https://konnectinsights.com/x-enterprise-partner/) · [Release Notes archive](https://konnectinsights.com/release-notes/) (17 notes, Jun 2023–Jul 2026) · [Developer portal](https://developer.konnectinsights.com/)

**Marketplaces:** [Salesforce AppExchange](https://appexchange.salesforce.com/appxListingDetail?listingId=a0N4V00000HNwWAUA1) · [Zendesk](https://www.zendesk.com/marketplace/apps/support/253251/konnect-insights/) · [Freshworks](https://www.freshworks.com/apps/freshdesk/konnect_insights) · [Fivetran connector docs](https://fivetran.com/docs/connectors/applications/konnect-insights) · [Zapier](https://zapier.com/apps/konnect-insights/integrations)

**Reviews & company:** [G2](https://www.g2.com/products/konnect-insights/reviews) (4.4/5, 36 reviews) · [Capterra](https://www.capterra.com/p/178918/Konnect-Insights/reviews/) (4.6/5, 19 reviews) · [SaaS Club — Sameer Narkar interview](https://saasclub.io/podcast/konnect-insights-sameer-narkar-431/) · [Latka](https://getlatka.com/companies/konnectinsights) · [Indian Startup Times, 17 Jun 2026](https://www.indianstartuptimes.com/news/bootstrapped-saas-firm-konnect-insights-nears-10-million-arr-as-global-expansion-gains-momentum/)

**Data acquisition:** [X API pricing](https://docs.x.com/x-api/getting-started/pricing) · [Meta Content Library](https://transparency.meta.com/researchtools/meta-content-library/) · [CrowdTangle shutdown](https://transparency.meta.com/researchtools/other-data-catalogue/crowdtangle/) · [YouTube quota](https://www.getphyllo.com/post/youtube-api-limits-how-to-calculate-api-usage-cost-and-fix-exceeded-api-quota) · [Reddit API pricing](https://www.socialcrawl.dev/blog/reddit-data-api-2026) · [TikTok Research API](https://developers.tiktok.com/products/research-api/) · [Google Business Profile API](https://slashpost.ai/blogs/google-business-profile/google-business-profile-api-documentation-2026)

**Legal & compliance:** [hiQ v. LinkedIn — lessons](https://www.zwillgen.com/alternative-data/hiq-v-linkedin-wrapped-up-web-scraping-lessons-learned/) · [Meta v. Bright Data](https://www.fbm.com/publications/major-decision-affects-law-of-scraping-and-online-data-collection-meta-platforms-v-bright-data/) · [DPDP 2026 milestones](https://www.mondaq.com/india/data-protection/1830402/dpdp-act-and-rules-2025-the-2026-compliance-milestones-businesses-cant-afford-to-miss) · [EY — DPDP Rules 2025](https://www.ey.com/en_in/insights/cybersecurity/transforming-data-privacy-digital-personal-data-protection-rules-2025)

**India NLP & WhatsApp:** [AI4Bharat IndicNLP Catalog](https://ai4bharat.github.io/indicnlp_catalog/) · [WhatsApp API pricing India 2026](https://codingclave.com/guides/whatsapp-api-pricing-india-2026-comparison)

**Internal:** `VedicReport/README.md`, `REPORT_TOOL_ANSWERS.md`, `RULEBOOK.md`, `portal/scraper.py`, `portal/schema.py`, `portal/queries.py`, `webapp/report_types.py`, `metrics/README.md`, `docs/v3-plan.md`
