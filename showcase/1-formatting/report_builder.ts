import {
  emptySection,
  formatFooter,
  summarize,
  toCells,
} from "./sections";
import type { ReportOptions, Row, Section } from "./types";

/**
 * Builders for each section of the monthly finance report. Every
 * builder takes the month's rows and the report options, and returns
 * one finished section.
 */

const COLUMNS = ["date", "account", "amount", "status"];
const GRACE = 30;
const BIG_REFUND = 50_000;
const DAY = 24 * 60 * 60 * 1000;

/** Orders rows by the time they were posted, oldest first. */
function byPosted(a: Row, b: Row): number {
  return a.postedAt - b.postedAt;
}

/** Rows of one kind, oldest first. */
function rowsOf(rows: readonly Row[], kind: string): Row[] {
  return rows.filter((row) => row.kind === kind).sort(byPosted);
}

/** Sum of the amounts of `rows`, in minor units. */
function totalOf(rows: readonly Row[]): number {
  return rows.reduce((sum, row) => sum + row.amount, 0);
}

/** Whether a refund is large enough to flag for review. */
function isBigRefund(row: Row): boolean {
  return row.amount > BIG_REFUND;
}

/** Builds the Orders section: totals, average, and largest order. */
export function buildOrderSection(
  rows: readonly Row[],
  opts: ReportOptions,
): Section {
  const title = "Orders";
  const visible = rowsOf(rows, "order");
  if (visible.length === 0) {
    return emptySection(title);
  }

  const totals = totalOf(visible);
  const average = totals / visible.length;
  const largest = Math.max(...visible.map((row) => row.amount));
  const summary = summarize({ totals, average, largest }, opts.locale);
  const footer = formatFooter(opts.locale, visible.length);

  return {
    title,
    columns: COLUMNS,
    rows: visible.map((row) => toCells(row, COLUMNS)),
    summary,
    footer,
  };
}

/** Builds the Refunds section, flagging any refund over the limit. */
export function buildRefundSection(
  rows: readonly Row[],
  opts: ReportOptions,
): Section {
  const title = "Refunds";
  const visible = rowsOf(rows, "refund");
  if (visible.length === 0) {
    return emptySection(title);
  }

  const big = visible.filter(isBigRefund).map((row) => row.id);
  const totals = totalOf(visible);
  const notes = big.length > 0 ? [`${big.length} flagged`] : [];

  return {
    title,
    columns: COLUMNS,
    rows: visible.map((row) => toCells(row, COLUMNS)),
    summary: summarize({ totals, flagged: big.length }, opts.locale),
    footer: formatFooter(opts.locale, visible.length),
    notes,
  };
}

/** Builds the Late invoices section from invoices past due. */
export function buildLateInvoiceSection(
  rows: readonly Row[],
  opts: ReportOptions,
  today: number,
): Section {
  const title = "Late invoices";
  const late = rowsOf(rows, "invoice").filter(
    (row) => row.status !== "paid" && today - row.dueAt > GRACE * DAY
  );
  if (late.length === 0) {
    return emptySection(title);
  }

  const oldest = late[0];
  const owed = totalOf(late);
  const summary = summarize({ owed, since: oldest.dueAt }, opts.locale);

  return {
    title,
    columns: [...COLUMNS, "days late"],
    rows: late.map((row) => [
      ...toCells(row, COLUMNS),
      String(Math.floor((today - row.dueAt) / DAY)),
    ]),
    summary,
    footer: formatFooter(opts.locale, late.length),
  };
}

/** Builds the Payouts section, one row per payee. */
export function buildPayoutSection(
  rows: readonly Row[],
  opts: ReportOptions,
): Section {
  const title = "Payouts";
  const byPayee = new Map<string, number>();
  for (const row of rowsOf(rows, "payout")) {
    const before = byPayee.get(row.account) ?? 0;
    byPayee.set(row.account, before + row.amount);
  }
  if (byPayee.size === 0) {
    return emptySection(title);
  }

  const payees = [...byPayee].map(([id, amount]) => ({
    id,
    amount
  }));
  payees.sort((a, b) => b.amount - a.amount);
  const totals = payees.reduce((sum, payee) => sum + payee.amount, 0);
  const columns = ["account", "amount"];

  return {
    title,
    columns,
    rows: payees.map((payee) => [payee.id, String(payee.amount)]),
    summary: summarize({ totals, payees: payees.length }, opts.locale),
    footer: formatFooter(opts.locale, payees.length),
  };
}

/** The share of `part` in `whole`, as a percentage with one decimal. */
function percentOf(part:number, whole:number): string {
  if (whole===0) return "0.0";
  return ((part/whole)*100).toFixed(1);
}

/** Builds the Fees section: each fee type's total and its share. */
export function buildFeeSection(
  rows: readonly Row[],
  opts: ReportOptions,
): Section {
  const title = "Fees";
  const fees = rowsOf(rows, "fee");
  if (fees.length === 0) {
    return emptySection(title);
  }

  const all = totalOf(fees);
  const types = new Set(fees.map((row) => row.feeType));
  const lines: string[][] = [];
  for (const type of types) {
    const total = totalOf(fees.filter((row) => row.feeType === type));
    lines.push([
      type,
      String(total),
      percentOf(total, all)
    ]);
  }

  return {
    title,
    columns: ["fee", "amount", "share"],
    rows: lines,
    summary: summarize({ totals: all, types: types.size }, opts.locale),
    footer: formatFooter(opts.locale, fees.length),
  };
}

/** Builds the Disputes section, opened and resolved this month. */
export function buildDisputeSection(
  rows: readonly Row[],
  opts: ReportOptions,
): Section {
  const title = "Disputes";
  const disputes = rowsOf(rows, "dispute");
  if (disputes.length === 0) {
    return emptySection(title);
  }

  const open = disputes.filter((row) => row.status === "open");
  const won = disputes.filter((row) => row.status === "won");
  const lost = disputes.filter((row) => row.status === "lost");
  const summary = summarize(
    { open: open.length, won: won.length, lost: lost.length },
    opts.locale
  );

  return {
    title,
    columns: COLUMNS,
    rows: disputes.map((row) => toCells(row, COLUMNS)),
    summary,
    footer: formatFooter(opts.locale, disputes.length),
  };
}

/** Builds the Currencies section: totals per settlement currency. */
export function buildCurrencySection(
  rows: readonly Row[],
  opts: ReportOptions,
): Section {
  const title = "Currencies";
  const totals = new Map<string, number>();
  for (const row of rows) {
    const before = totals.get(row.currency) ?? 0;
    totals.set(row.currency, before + row.amount);
  }

  const codes = [...totals.keys()].sort();
  const all = totalOf(rows);
  const columns = ["currency", "amount", "share",
    "change", "previous"];
  const cells = codes.map((code) => {
    const amount = totals.get(code) ?? 0;
    const previous = opts.previous.get(code) ?? 0;
    return [
      code,
      String(amount),
      percentOf(amount, all),
      String(amount - previous),
      String(previous),
    ];
  });

  return {
    title,
    columns,
    rows: cells,
    summary: summarize({ currencies: codes.length }, opts.locale),
    footer: formatFooter(opts.locale, codes.length),
  };
}

/** Whether a section is worth printing on the cover page. */
export function isHeadline(
  section: Section,
  opts: ReportOptions,
): boolean {
  if (section.rows.length === 0) {
    return false;
  }
  if (opts.headlines.includes(section.title)) {
    return true;
  }

  const limit = opts.disputeLimit;
  return (section.title === "Disputes" && section.rows.length > limit);
}

/** Builds every section of the report in print order. */
export function buildReport(
  rows: readonly Row[],
  opts: ReportOptions,
  today: number,
): Section[] {
    const sections = [
        buildOrderSection(rows, opts),
        buildRefundSection(rows, opts),
        buildLateInvoiceSection(rows, opts, today),
        buildPayoutSection(rows, opts),
        buildFeeSection(rows, opts),
        buildDisputeSection(rows, opts),
        buildCurrencySection(rows, opts),
    ];
  const headlines = sections.filter((s) => isHeadline(s, opts));
  const rest = sections.filter((s) => !isHeadline(s, opts));
  return [...headlines, ...rest];
}
