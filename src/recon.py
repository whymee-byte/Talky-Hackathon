"""Bank reconciliation matcher for the Talky x Anthropic hackathon.

    python src/recon.py --phase sample --out out/sub_sample.csv.gz
    python src/recon.py --phase train  --out out/sub_train.csv.gz
    python src/recon.py --phase live   --out out/sub_live.csv.gz
"""
import argparse, csv, glob, gzip, math, os, re, sys, time, unicodedata
from collections import defaultdict, Counter
from datetime import date, timedelta

import pandas as pd

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "bankrec-docs-and-masters")

# ----------------------------------------------------------------------------- helpers

def C(x):
    """amount string -> integer cents"""
    if x is None or x == "" or (isinstance(x, float) and math.isnan(x)):
        return 0
    return int(round(float(x) * 100))


def D(s):
    return date.fromisoformat(s[:10]) if isinstance(s, str) and s else None


def strip_acc(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", strip_acc(s).upper())


def digits(s):
    return re.sub(r"\D", "", s or "")


LEGAL = set("SL SLU SA SAU SCOOP SC SRL SARL EURL SAS SNC GMBH CO KG EK AG UG LTD LIMITED PLC INC LLC CORP LDA SPA SPA BV NV OHG GBR Y E DE DEL LA LOS LAS EL THE AND ET UND DES DU LE GROUP GRUPO".split())
IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{10,30})\b")
TOK_RE = re.compile(r"[A-Z0-9][A-Z0-9/\-\.]*[A-Z0-9]|[A-Z0-9]")


def name_tokens(s):
    toks = re.findall(r"[A-Z0-9]+", strip_acc(s or "").upper())
    return [t for t in toks if t not in LEGAL and len(t) >= 2]


def tol(golden_cents):
    return max(1, int(abs(golden_cents) * 0.005))


def read(path, **kw):
    if not os.path.exists(path):
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, keep_default_na=False, **kw)


# ----------------------------------------------------------------------------- data loading

class Data:
    def __init__(self, phase, root=ROOT):
        t0 = time.time()
        self.phase = phase
        self.root = root
        folders = {"sample": ["sample"], "train": ["train"], "live": ["train", "live"], "final": ["train", "live", "final"]}[phase]
        self.folder = os.path.join(root, phase)
        m = os.path.join(root, "masters")
        if phase == "final" and os.path.exists(os.path.join(self.folder, "masters_final")):
            m = os.path.join(self.folder, "masters_final")
        self.customers = read(os.path.join(m, "customers.csv"))
        self.suppliers = read(os.path.join(m, "suppliers.csv"))
        self.accounts = read(os.path.join(m, "bank_accounts.csv"))
        self.terms = {r.code: r for r in read(os.path.join(m, "payment_terms.csv")).itertuples()}
        self.categories = list(read(os.path.join(m, "categories.csv"))["category"])
        self.fx = self._load_fx(os.path.join(m, "fx_rates.csv"))
        # --- items
        items = []
        for f in folders:
            p = os.path.join(root, f)
            ar = read(os.path.join(p, "ar_invoices.csv.gz"))
            if len(ar):
                items.append(pd.DataFrame({"item_id": ar.item_id, "entity": ar.entity, "party": ar.customer_id, "pname": ar.customer_name,
                                           "doc": ar.document_number, "date": ar.issue_date, "due": ar.due_date, "cur": ar.currency,
                                           "total": ar.total_amount, "terms": ar.payment_terms, "po": ar.customer_po, "pm": ar.payment_method, "rel": ""}))
            apd = read(os.path.join(p, "ap_invoices.csv.gz"))
            if len(apd):
                items.append(pd.DataFrame({"item_id": apd.item_id, "entity": apd.entity, "party": apd.supplier_id, "pname": apd.supplier_name,
                                           "doc": apd.supplier_invoice_number, "date": apd.issue_date, "due": apd.due_date, "cur": apd.currency,
                                           "total": apd.total_amount, "terms": apd.payment_terms, "po": apd.our_po, "pm": apd.payment_method, "rel": ""}))
            cn = read(os.path.join(p, "credit_notes.csv.gz"))
            if len(cn):
                items.append(pd.DataFrame({"item_id": cn.item_id, "entity": cn.entity, "party": cn.party_id, "pname": cn.party_name,
                                           "doc": cn.document_number, "date": cn.issue_date, "due": cn.issue_date, "cur": cn.currency,
                                           "total": cn.total_amount, "terms": "", "po": "", "pm": "", "rel": cn.related_document}))
        it = pd.concat(items, ignore_index=True).drop_duplicates("item_id", keep="last")
        oi = read(os.path.join(self.folder, "open_items_at_start.csv.gz"))
        if len(oi):
            missing = oi[~oi.item_id.isin(it.item_id)]
            if len(missing):
                it = pd.concat([it, pd.DataFrame({"item_id": missing.item_id, "entity": missing.entity, "party": missing.party_id, "pname": "",
                                                  "doc": missing.document_number, "date": missing.issue_date, "due": missing.due_date,
                                                  "cur": missing.currency, "total": missing.total_amount, "terms": "", "po": "", "pm": "", "rel": ""})],
                               ignore_index=True)
        it["kind"] = it.item_id.str[:3]
        self.items = {}
        for r in it.itertuples(index=False):
            self.items[r.item_id] = r
        # --- open balances
        self.open = {}
        if len(oi):
            cur_items = set()
            for ph in [phase]:
                for name in ("ar_invoices", "ap_invoices", "credit_notes"):
                    df = read(os.path.join(root, ph, f"{name}.csv.gz"))
                    if len(df):
                        cur_items.update(df.item_id)
            for r in oi.itertuples():
                self.open[r.item_id] = C(r.open_amount)
            for i in cur_items:
                if i not in self.open:
                    self.open[i] = C(self.items[i].total)
        else:
            for i, r in self.items.items():
                self.open[i] = C(r.total)
        self.total = {i: C(r.total) for i, r in self.items.items()}
        self.settled_by = defaultdict(list)  # item -> [(bank_txn_id, amount)]
        # --- indexes
        self.by_norm = defaultdict(list)        # normalized doc -> items
        self.by_party = defaultdict(list)       # party -> items
        for i, r in self.items.items():
            n = norm(r.doc)
            if n:
                self.by_norm[n].append(i)
                dg = digits(r.doc).lstrip("0")
                if dg and dg != n:
                    self.by_norm["#" + dg].append(i)
            self.by_party[r.party].append(i)
        for p in self.by_party:
            self.by_party[p].sort(key=lambda i: (self.items[i].date, i))
        self.cancelled = {}   # filled from PDFs later
        # --- parties
        self.party_name = {}
        self.party_entity = {}
        self.iban_party = defaultdict(set)
        self.mandate_party = {}
        self.group_members = defaultdict(set)
        self.party_group = {}
        for r in self.customers.itertuples():
            self.party_name[r.customer_id] = r.name
            self.party_entity[r.customer_id] = r.entity
            if r.iban:
                self.iban_party[r.iban].add(r.customer_id)
            if r.sepa_mandate_id:
                self.mandate_party[r.sepa_mandate_id] = r.customer_id
            if r.group_id:
                self.group_members[r.group_id].add(r.customer_id)
                self.party_group[r.customer_id] = r.group_id
        for r in self.suppliers.itertuples():
            self.party_name[r.supplier_id] = r.name
            self.party_entity[r.supplier_id] = r.entity
            if r.iban:
                self.iban_party[r.iban].add(r.supplier_id)
        self.cust_trade = {r.customer_id: r.trade_name for r in self.customers.itertuples() if r.trade_name}
        self.acc = {r.account_id: r for r in self.accounts.itertuples()}
        self._build_name_index()
        # --- batch files (this phase + previous phases for returns)
        self.dd_rows = defaultdict(list)        # batch_id -> rows
        self.dd_by_mandate = defaultdict(list)  # mandate -> rows
        self.card = defaultdict(list)           # (account, settlement_id) -> rows
        self.cheque = defaultdict(list)         # deposit_id -> rows
        self.runs = defaultdict(list)           # run_id -> rows
        self.e2e = defaultdict(list)            # end_to_end_id -> rows
        for f in folders:
            p = os.path.join(root, f)
            for fn in glob.glob(os.path.join(p, "direct_debit_batches", "*.csv")):
                for r in read(fn).itertuples():
                    self.dd_rows[r.batch_id].append(r)
                    self.dd_by_mandate[r.mandate_id].append(r)
            if f != phase and phase != "sample":
                continue
            for fn in glob.glob(os.path.join(p, "card_settlements", "*.csv")):
                for r in read(fn).itertuples():
                    self.card[(r.account_id, r.settlement_id)].append(r)
            for fn in glob.glob(os.path.join(p, "cheque_deposits", "*.csv")):
                for r in read(fn).itertuples():
                    self.cheque[r.deposit_id].append(r)
            for fn in glob.glob(os.path.join(p, "payment_runs", "*.csv")):
                for r in read(fn).itertuples():
                    self.runs[r.run_id].append(r)
                    self.e2e[r.end_to_end_id].append(r)
        self.card_by_acc = defaultdict(list)
        for (acc, sid), rows in self.card.items():
            self.card_by_acc[acc].append((sid, rows))
        print(f"loaded {phase}: {len(self.items)} items, {sum(1 for v in self.open.values() if v)} open, "
              f"{len(self.dd_rows)} dd batches, {len(self.runs)} runs in {time.time()-t0:.1f}s", file=sys.stderr)

    def _load_fx(self, path):
        fx = defaultdict(dict)
        for r in read(path).itertuples():
            fx[r.currency][r.date] = float(r.units_per_eur)
        out = {}
        for cur, d in fx.items():
            out[cur] = (sorted(d), d)
        return out

    def rate(self, cur, day):
        """units of cur per EUR on day (last available)."""
        if cur == "EUR":
            return 1.0
        if cur not in self.fx:
            return None
        keys, d = self.fx[cur]
        import bisect
        k = bisect.bisect_right(keys, day) - 1
        return d[keys[max(k, 0)]]

    def _build_name_index(self):
        self.tok_party = defaultdict(lambda: defaultdict(set))  # entity -> token -> parties
        self.party_toks = {}
        for p, name in self.party_name.items():
            ent = self.party_entity[p]
            toks = set(name_tokens(name)) | set(name_tokens(self.cust_trade.get(p, "")))
            self.party_toks[p] = toks
            for t in toks:
                self.tok_party[ent][t].add(p)


# ----------------------------------------------------------------------------- non-item classifier (naive Bayes on tokens)

def line_feats(t):
    txt = strip_acc(" ".join([t["text"], t["ref2"], t["remittance_info"], t["counterparty_name"]])).upper()
    toks = set(w for w in re.findall(r"[A-Z]{3,}", txt))
    f = {"code=" + t["bank_code"], "sub=" + t["bank_code"] + "/" + t["bank_subcode"], "sign=" + ("+" if C(t["amount"]) > 0 else "-"),
         "fmt=" + t["statement_format"]}
    f |= {"w=" + w for w in toks}
    if not t["counterparty_name"] and not t["counterparty_account"]:
        f.add("nocp")
    return f


class NB:
    def __init__(self):
        self.cls = Counter()
        self.feat = defaultdict(Counter)
        self.vocab = set()

    def fit(self, rows):
        for f, y in rows:
            self.cls[y] += 1
            for x in f:
                self.feat[y][x] += 1
            self.vocab |= f
        self.tot = {y: sum(c.values()) for y, c in self.feat.items()}
        self.n = sum(self.cls.values())

    def predict(self, f):
        best = []
        V = len(self.vocab) + 1
        for y, ny in self.cls.items():
            lp = math.log(ny / self.n)
            fy = self.feat[y]
            for x in f:
                if x in self.vocab:
                    lp += math.log((fy.get(x, 0) + 0.1) / (ny + 0.2))
                    # bernoulli-ish: presence only
            best.append((lp, y))
        best.sort(reverse=True)
        m = best[0][0]
        z = sum(math.exp(b[0] - m) for b in best)
        return best[0][1], 1.0 / z, best


def train_classifier(root=ROOT, max_lines=None, phase_folder="train"):
    """Learn ITEM vs category from TRAIN golden."""
    import pickle
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out", "nb.pkl")
    if os.path.exists(cache):
        nb = NB()
        nb.cls, nb.feat, nb.vocab, nb.tot, nb.n = pickle.load(open(cache, "rb"))
        return nb
    t0 = time.time()
    g = read(os.path.join(root, phase_folder, "golden_allocations.csv.gz"), usecols=["bank_txn_id", "item_id", "category"])
    lab = {}
    for b, i, c in zip(g.bank_txn_id, g.item_id, g.category):
        lab[b] = "ITEM" if i else c
    rows = []
    with gzip.open(os.path.join(root, phase_folder, "bank_transactions.csv.gz"), "rt", encoding="utf-8") as fh:
        for k, t in enumerate(csv.DictReader(fh)):
            y = lab.get(t["bank_txn_id"])
            if y is None:
                continue
            rows.append((line_feats(t), y))
    nb = NB()
    nb.fit(rows)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    pickle.dump((nb.cls, dict(nb.feat), nb.vocab, nb.tot, nb.n), open(cache, "wb"))
    print(f"classifier trained on {len(rows)} lines in {time.time()-t0:.0f}s", file=sys.stderr)
    return nb


# ----------------------------------------------------------------------------- matcher

EXC_FULL = {"SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING", "FX_DIFFERENCE", "OVERPAYMENT"}


class Matcher:
    def __init__(self, data: Data, nb: NB):
        self.d = data
        self.nb = nb
        self.stats = Counter()

    # ---------------- party identification
    def party_from_text(self, t, ent_list, side):
        d = self.d
        cands = Counter()
        txt = " ".join([t["counterparty_name"], t["text"], t["remittance_info"], t["ref2"]])
        # IBAN
        ibans = set(IBAN_RE.findall(norm_spaces(txt))) | ({t["counterparty_account"]} if t["counterparty_account"] else set())
        for ib in ibans:
            for p in d.iban_party.get(ib, ()):
                if d.party_entity.get(p) in ent_list and p[0] == side:
                    return p, 1.0
        # mandate
        for mm in re.findall(r"VTM-[A-Z]{2}-\d{7}", txt):
            p = d.mandate_party.get(mm)
            if p:
                return p, 1.0
        toks = name_tokens(txt)
        if not toks:
            return None, 0
        tokset = set(toks)
        last = toks[-1] if toks else ""
        scores = Counter()
        for ent in ent_list:
            idx = d.tok_party.get(ent, {})
            for tk in tokset:
                ps = idx.get(tk)
                if ps and len(ps) < 400:
                    w = 1.0 / math.log(2 + len(ps))
                    for p in ps:
                        if p[0] == side:
                            scores[p] += w
        if not scores:
            return None, 0
        best = []
        for p, s in scores.most_common(25):
            ptoks = d.party_toks[p]
            if not ptoks:
                continue
            tot = sum(1.0 / math.log(2 + len(d.tok_party[d.party_entity[p]].get(x, ()))) for x in ptoks)
            hit = 0.0
            for x in ptoks:
                w = 1.0 / math.log(2 + len(d.tok_party[d.party_entity[p]].get(x, ())))
                if x in tokset:
                    hit += w
                elif len(x) >= 4 and any(y.startswith(x[:4]) and len(y) >= 3 and x.startswith(y) for y in tokset):
                    hit += 0.8 * w   # truncated
            best.append((hit / tot, s, p))
        best.sort(reverse=True)
        if best and best[0][0] >= 0.6:
            if len(best) > 1 and best[1][0] >= best[0][0] - 1e-9 and best[1][2] != best[0][2]:
                return best[0][2], best[0][0] * 0.5
            return best[0][2], best[0][0]
        return None, 0

    # ---------------- references
    def ref_items(self, t, ent_list, kinds, party=None):
        d = self.d
        txt = strip_acc(" ".join([t["text"], t["ref1"], t["ref2"], t["remittance_info"]])).upper()
        found = []
        seen = set()
        raw = TOK_RE.findall(txt)
        toks = set(raw)
        # also join tokens split by N43 boundary " | "
        joined = txt.replace(" | ", "")
        toks |= set(TOK_RE.findall(joined))
        for tk in toks:
            n = norm(tk)
            if len(n) < 4:
                continue
            for key in (n, "#" + digits(n).lstrip("0")):
                if key == "#" or len(key) < 5:
                    continue
                for i in d.by_norm.get(key, ()):
                    r = d.items[i]
                    if r.entity in ent_list and r.kind in kinds and i not in seen and (party is None or r.party == party or key == n):
                        seen.add(i)
                        found.append(i)
        return found

    def suffix_items(self, t, cand):
        """digits tokens of the text that are a suffix of a candidate item's number (len>=4) or 1-typo."""
        d = self.d
        txt = strip_acc(" ".join([t["text"], t["ref1"], t["ref2"], t["remittance_info"]])).upper()
        dtoks = set(x.lstrip("0") for x in re.findall(r"\d{4,}", txt))
        dtoks.discard("")
        out = []
        for i in cand:
            dg = digits(d.items[i].doc).lstrip("0")
            if len(dg) < 4:
                continue
            for x in dtoks:
                if len(x) >= 4 and (dg.endswith(x) or (len(x) >= 6 and dg[-len(x):] != x and _near(dg[-len(x):], x))):
                    out.append(i)
                    break
        return out

    # ---------------- main
    def run(self, txns):
        out = []
        for t in txns:
            rows = self.match_line(t)
            if not rows:
                rows = [self.no_item(t)]
            out.extend(rows)
        return out

    def no_item(self, t, force_cat=None):
        amt = C(t["amount"])
        if force_cat:
            cat = force_cat
        else:
            y, p, best = self.nb.predict(line_feats(t))
            cats = [b[1] for b in best if b[1] != "ITEM"]
            cat = cats[0] if cats else ("UNIDENTIFIED" if amt > 0 else "BANK_FEES")
        exc = "UNIDENTIFIED" if cat == "UNIDENTIFIED" else ""
        self.stats["cat:" + cat] += 1
        return [t["bank_txn_id"], "", "", cat, exc]

    def apply(self, b, alloc):
        """alloc: list of (item, amount_cents, exception). Updates open balances."""
        d = self.d
        rows = []
        seen = set()
        for i, a, e in alloc:
            if i in seen:
                continue
            seen.add(i)
            if e == "RETURNED":
                d.open[i] = d.open.get(i, 0) + a
            elif e in EXC_FULL or e == "":
                d.open[i] = 0 if e != "" else max(0, d.open.get(i, 0) - a)
            else:
                d.open[i] = max(0, d.open.get(i, 0) - a)
            d.settled_by[i].append((b, a))
            rows.append([b, i, f"{a/100:.2f}", "", e])
        return rows

    def entities_for(self, acc_ent, side):
        if acc_ent == "ES00":
            return ["ES00", "ES01", "ES02", "PT01"]
        return [acc_ent]

    def match_line(self, t):
        d = self.d
        b = t["bank_txn_id"]
        amt = C(t["amount"])
        acc = d.acc.get(t["account_id"])
        ent = t["entity"]
        txt = strip_acc(" ".join([t["text"], t["ref1"], t["ref2"], t["remittance_info"]])).upper()
        day = t["booking_date"]

        # 0. classifier: strongly non-item lines
        y, p, best = self.nb.predict(line_feats(t))
        if y != "ITEM" and p > 0.97 and y not in ("UNIDENTIFIED", "CUSTOMER_REFUND", "SUPPLIER_REFUND", "CUSTOMER_ADVANCE"):
            self.stats["nb_direct"] += 1
            return [self.no_item(t, y)]

        # 1. DD return
        if amt < 0 and re.search(r"DEVOLUCION RECIBO|IMPAYE|RUECKLASTSCHRIFT|RÜCKLASTSCHRIFT|RETURN|R-TRANSAC|UNPAID|RUCKLAST", txt):
            r = self.match_return(t, txt, amt)
            if r:
                return r
        # 2. DD batch credit
        for bid in re.findall(r"\b(\d{11})\b", txt):
            if bid in d.dd_rows:
                rows = d.dd_rows[bid]
                alloc = []
                for r in rows:
                    its = [i for i in d.by_norm.get(norm(r.remittance_info), ()) if d.items[i].kind == "ARI"]
                    if its:
                        alloc.append((its[0], C(r.amount), ""))
                if alloc:
                    self.stats["dd_batch"] += 1
                    return self.apply(b, alloc)
        # 3. cheque deposit
        for dep in re.findall(r"\b(RCH\d+|[A-Z]{2,4}\d{8,})\b", txt):
            if dep in d.cheque:
                alloc = []
                for r in d.cheque[dep]:
                    i = self.find_doc(r.stub_reference, [ent], ("ARI",), C(r.amount), drawer=r.drawer)
                    if i:
                        alloc.append((i, C(r.amount), ""))
                if alloc:
                    self.stats["cheque"] += 1
                    return self.apply(b, alloc)
        # 4. card settlement
        if amt > 0 and t["account_id"] in d.card_by_acc:
            for sid, rows in d.card_by_acc[t["account_id"]]:
                if abs(sum(C(r.net) for r in rows) - amt) <= 1 and abs((D(rows[0].settlement_date) - D(day)).days) <= 3:
                    alloc = []
                    for r in rows:
                        its = d.by_norm.get(norm(r.order_number), ())
                        if its:
                            alloc.append((its[0], C(r.gross), ""))
                    if alloc:
                        self.stats["card"] += 1
                        return self.apply(b, alloc)
        # 5. payment runs (our outgoing payments)
        if amt < 0:
            r = self.match_run(t, txt, amt)
            if r:
                return r

        # 6. generic receipt / payment
        side = "C" if amt > 0 else "S"
        kinds = ("ARI", "ARC") if side == "C" else ("API", "APC")
        ents = self.entities_for(ent, side)
        party, conf = self.party_from_text(t, ents, side)
        refs = self.ref_items(t, ents, kinds, party)
        if party:
            refs_p = [i for i in refs if d.items[i].party == party]
            if refs_p or not refs:
                refs = refs_p
        if not refs and party:
            cand = [i for i in d.by_party.get(party, ()) if d.open.get(i, 0) > 0 and d.items[i].kind in kinds]
            refs = self.suffix_items(t, cand)
        if refs:
            res = self.alloc_refs(t, refs, amt, side)
            if res:
                self.stats["refs"] += 1
                return self.apply(b, res)
        if party:
            res = self.alloc_party(t, party, amt, side, kinds)
            if res:
                self.stats["party"] += 1
                return self.apply(b, res)
        self.stats["nomatch_" + side] += 1
        if side == "C":
            # classifier may know (advance, refund...)
            if y != "ITEM" and p > 0.6:
                return [self.no_item(t, y)]
            return [self.no_item(t, "UNIDENTIFIED")]
        return None

    def find_doc(self, ref, ents, kinds, amount=None, drawer=None):
        d = self.d
        n = norm(ref)
        for key in (n, "#" + digits(n).lstrip("0")):
            if len(key) < 4:
                continue
            its = [i for i in d.by_norm.get(key, ()) if d.items[i].kind in kinds and d.items[i].entity in ents]
            if its:
                return its[0]
        return None

    def match_return(self, t, txt, amt):
        d = self.d
        b = t["bank_txn_id"]
        mand = re.findall(r"VTM-[A-Z]{2}-\d{7}", txt)
        batches = re.findall(r"\b(\d{11})\b", txt)
        cand = []
        for m in mand:
            for r in d.dd_by_mandate.get(m, ()):
                if C(r.amount) == -amt:
                    cand.append(r)
        if batches:
            c2 = [r for r in cand if r.batch_id in batches]
            cand = c2 or cand
        if cand:
            r = sorted(cand, key=lambda r: r.collection_date)[-1]
            i = self.find_doc(r.remittance_info, [t["entity"]], ("ARI",))
            if i:
                self.stats["return"] += 1
                return self.apply(b, [(i, -amt, "RETURNED")])
        # fallback: party by mandate/name, settled item with that amount
        party = None
        for m in mand:
            party = d.mandate_party.get(m)
        if not party:
            party, _ = self.party_from_text(t, [t["entity"]], "C")
        if party:
            its = [i for i in d.by_party.get(party, ()) if d.total[i] == -amt and d.items[i].kind == "ARI" and d.open.get(i, 0) == 0]
            if its:
                self.stats["return_fb"] += 1
                return self.apply(b, [(its[-1], -amt, "RETURNED")])
        return None

    def match_run(self, t, txt, amt):
        d = self.d
        b = t["bank_txn_id"]
        rows = None
        e2e = t["end_to_end_id"]
        if e2e and e2e in d.e2e:
            rows = d.e2e[e2e]
        if rows is None:
            for tk in re.findall(r"\b((?:PR|CF)\d{10,}(?:-\d{4})?)\b", txt):
                if tk in d.e2e:
                    rows = d.e2e[tk]
                    break
                if tk in d.runs:
                    rr = d.runs[tk]
                    # aggregated: whole run; confirming: one debit per maturity -> select rows summing to amt
                    tot = sum(C(x.document_amount) for x in rr)
                    if abs(tot + amt) <= 1:
                        rows = rr
                    else:
                        byday = defaultdict(list)
                        for x in rr:
                            byday[x.execution_date].append(x)
                        for k, v in byday.items():
                            if abs(sum(C(x.document_amount) for x in v) + amt) <= 1:
                                rows = v
                                break
                        if rows is None:
                            rows = rr
                    break
        if not rows:
            return None
        alloc = []
        for r in rows:
            kinds = ("APC",) if r.document_type.upper().startswith("CREDIT") else ("API",)
            ents = self.entities_for(t["entity"], "S") + [d.acc[r.debtor_account_id].entity] if r.debtor_account_id in d.acc else self.entities_for(t["entity"], "S")
            n = norm(r.document_number)
            its = [i for i in d.by_norm.get(n, ()) if d.items[i].kind in kinds]
            if len(its) > 1:
                bn = set(name_tokens(r.beneficiary))
                its2 = [i for i in its if set(name_tokens(d.items[i].pname)) & bn]
                its = its2 or its
                its3 = [i for i in its if d.open.get(i, 0) > 0]
                its = its3 or its
            if its:
                alloc.append((its[0], abs(C(r.document_amount)), ""))
        if not alloc:
            return None
        self.stats["run"] += 1
        # partial: document amount < open
        out = []
        for i, a, e in alloc:
            o = d.open.get(i, 0)
            if d.items[i].kind == "API" and o > a + tol(a):
                e = "PARTIAL"
            out.append((i, a, e))
        return self.apply(b, out)

    # ---------------- allocation logic
    def cash_in_item_cur(self, t, item_cur, amt):
        """returns cash (cents, positive) expressed in item currency, and fx flag"""
        acur = t["currency"]
        if item_cur == acur:
            return abs(amt), False
        if t["orig_currency"] == item_cur and t["orig_amount"]:
            return abs(C(t["orig_amount"])), True
        r_acc = self.d.rate(acur, t["booking_date"])
        r_it = self.d.rate(item_cur, t["booking_date"])
        if r_acc and r_it:
            return int(round(abs(amt) / r_acc * r_it)), True
        return abs(amt), True

    def alloc_refs(self, t, refs, amt, side):
        d = self.d
        refs = sorted(set(refs), key=lambda i: (d.items[i].date, i))
        cur = d.items[refs[0]].cur
        cash, fx = self.cash_in_item_cur(t, cur, amt)
        sign = 1 if side == "C" else -1
        inv = [i for i in refs if d.items[i].kind in ("ARI", "API")]
        cns = [i for i in refs if d.items[i].kind in ("ARC", "APC")]
        # value of each: open amount; if fully settled already -> duplicate candidate
        def val(i):
            o = d.open.get(i, 0)
            return o
        tot = sum(val(i) for i in inv) - sum(val(i) for i in cns)
        if tot <= 0:
            # all refs already settled -> duplicate payment of exact total?
            if len(inv) == 1 and abs(d.total[inv[0]] - cash) <= tol(cash):
                return [(inv[0], cash, "DUPLICATE_PAYMENT")]
            # supplier refund of credit note
            if cns and not inv:
                return [(i, d.total[i], "") for i in cns]
            return None
        return self.explain(t, inv, cns, cash, fx, side)

    def explain(self, t, inv, cns, cash, fx, side):
        """Given chosen invoices/credit notes and the cash (item currency), return allocations with exceptions."""
        d = self.d
        O = {i: d.open.get(i, 0) for i in inv + cns}
        tot = sum(O[i] for i in inv) - sum(O[i] for i in cns)
        diff = tot - cash  # >0 short, <0 over
        day = t["booking_date"]
        res = [(i, O[i], "") for i in inv] + [(i, O[i], "") for i in cns]
        fxe = "FX_DIFFERENCE" if fx else ""
        if fx:
            # FX tolerance 3%
            if abs(diff) <= 0.03 * tot:
                return [(i, O[i], fxe if i in inv else "") for i in inv] + [(i, O[i], "") for i in cns]
        if abs(diff) <= max(1, 0):  # exact
            return [(i, O[i], fxe if (fx and i in inv) else "") for i in inv] + [(i, O[i], "") for i in cns]
        if diff < 0:
            if len(inv) == 1:
                return [(inv[0], O[inv[0]], "OVERPAYMENT")] + [(i, O[i], "") for i in cns]
            return [(i, O[i], "") for i in inv] + [(i, O[i], "") for i in cns]
        # short payment: try per-invoice explanations
        # discount
        disc_total = 0
        disc_ok = []
        for i in inv:
            r = d.items[i]
            tm = d.terms.get(r.terms)
            if tm is not None and tm.early_payment_discount_pct:
                pct = float(tm.early_payment_discount_pct)
                dd = int(tm.discount_days or 0)
                dsc = int(round(d.total[i] * pct / 100))
                in_time = D(day) <= D(r.date) + timedelta(days=dd + 3)
                disc_ok.append((i, dsc, in_time))
                disc_total += dsc
        if disc_ok and abs(diff - disc_total) <= max(2, len(inv)):
            out = []
            for i in inv:
                m = [x for x in disc_ok if x[0] == i]
                if m and m[0][2]:
                    out.append((i, O[i], "SHORT_DISCOUNT" if not fx else fxe))
                elif m:
                    out.append((i, O[i] - m[0][1], "PARTIAL"))
                else:
                    out.append((i, O[i], fxe))
            return out + [(i, O[i], "") for i in cns]
        # withholding
        if len(inv) >= 1:
            for rate in (0.0125, 0.015, 0.10):
                w = sum(int(round(d.total[i] * rate)) for i in inv)
                if abs(diff - w) <= max(2, len(inv)):
                    return [(i, O[i], "SHORT_WITHHOLDING" if not fx else fxe) for i in inv] + [(i, O[i], "") for i in cns]
        # bank charges (non-SEPA)
        eur = diff / 100.0
        rr = d.rate(d.items[inv[0]].cur, day) or 1.0
        if diff / rr <= 6000 and diff <= 0.02 * tot and (fx or d.items[inv[0]].cur != "EUR" or t["currency"] != "EUR" or t["orig_currency"]):
            return [(i, O[i], "SHORT_BANK_CHARGES" if not fx else fxe) for i in inv] + [(i, O[i], "") for i in cns]
        # partial: shortage on the last invoice
        out = []
        remaining = cash + sum(O[i] for i in cns)
        for k, i in enumerate(inv):
            if k < len(inv) - 1:
                a = min(O[i], remaining)
                out.append((i, a, "" if a == O[i] else "PARTIAL"))
                remaining -= a
            else:
                if remaining <= 0:
                    break
                out.append((i, remaining, "PARTIAL"))
        return out + [(i, O[i], "") for i in cns]

    def alloc_party(self, t, party, amt, side, kinds):
        d = self.d
        cand = [i for i in d.by_party.get(party, ()) if d.open.get(i, 0) > 0 and d.items[i].kind in kinds]
        if not cand:
            return None
        cur = d.items[cand[0]].cur
        cash, fx = self.cash_in_item_cur(t, cur, amt)
        inv = [i for i in cand if d.items[i].kind in ("ARI", "API")]
        cns = [i for i in cand if d.items[i].kind in ("ARC", "APC")]
        t_ = tol(cash) if fx else 0
        # a. single invoice exact (FIFO)
        if not fx:
            ex = [i for i in inv if d.open[i] == cash]
            if ex:
                return [(ex[0], cash, "")]
        # b. FIFO prefix
        s = 0
        for k, i in enumerate(inv[:400]):
            s += d.open[i]
            if abs(s - cash) <= (int(0.03 * s) if fx else 0):
                return self.explain(t, inv[:k + 1], [], cash, fx, side)
            if s > cash * 1.05:
                break
        # c. subset sum (exact) incl credit notes
        sub = subset_sum([(i, d.open[i]) for i in inv[:60]] + [(i, -d.open[i]) for i in cns[:20]], cash, t_ if fx else 0)
        if sub:
            ii = [i for i in sub if d.items[i].kind in ("ARI", "API")]
            cc = [i for i in sub if d.items[i].kind in ("ARC", "APC")]
            return self.explain(t, ii, cc, cash, fx, side)
        # d. single invoice with explained shortfall (discount / withholding / bank charges)
        for i in inv[:200]:
            o = d.open[i]
            if o > cash and (o - cash) <= 0.1 * o + 1:
                r = self.explain(t, [i], [], cash, fx, side)
                if r and r[0][2] in ("SHORT_DISCOUNT", "SHORT_WITHHOLDING", "SHORT_BANK_CHARGES", "FX_DIFFERENCE"):
                    return r
        # e. discount subset: invoices with discount terms
        return None


def _near(a, b):
    if len(a) != len(b):
        return False
    diff = [k for k in range(len(a)) if a[k] != b[k]]
    if len(diff) == 1:
        return True
    if len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]:
        return True
    return False


def norm_spaces(s):
    return strip_acc(s or "").upper().replace(" ", "")


def subset_sum(items, target, tolerance=0, limit=300000):
    """items: [(id, cents)], find subset summing to target (± tolerance). Prefers earlier items (FIFO)."""
    neg = sum(v for _, v in items if v < 0)
    hi = target + tolerance - neg
    reach = {0: ()}
    offs = range(-tolerance, tolerance + 1) if tolerance <= 50 else (0,)
    for idx, (i, v) in enumerate(items):
        for o in offs:
            need = target + o - v
            if need in reach:
                return [items[k][0] for k in reach[need] + (idx,)]
        if len(reach) < limit:
            new = {}
            for s, path in reach.items():
                ns = s + v
                if ns <= hi and ns not in reach:
                    new[ns] = path + (idx,)
            reach.update(new)
    return None


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="sample")
    ap.add_argument("--out", required=True)
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    t0 = time.time()
    nb = train_classifier(a.root)
    d = Data(a.phase, a.root)
    m = Matcher(d, nb)
    with gzip.open(os.path.join(a.root, a.phase, "bank_transactions.csv.gz"), "rt", encoding="utf-8") as fh:
        txns = list(csv.DictReader(fh))
    txns.sort(key=lambda t: (t["booking_date"], t["bank_txn_id"]))
    if a.limit:
        txns = txns[: a.limit]
    rows = []
    for k, t in enumerate(txns):
        r = m.match_line(t) or [m.no_item(t)]
        rows.extend(r)
        if k % 50000 == 0 and k:
            print(f"  {k}/{len(txns)} {time.time()-t0:.0f}s", file=sys.stderr)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with gzip.open(a.out, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["bank_txn_id", "item_id", "amount", "category", "exception"])
        w.writerows(rows)
    print(f"{len(rows)} rows for {len(txns)} lines -> {a.out} in {time.time()-t0:.0f}s", file=sys.stderr)
    print(dict(m.stats.most_common(40)), file=sys.stderr)


if __name__ == "__main__":
    main()
