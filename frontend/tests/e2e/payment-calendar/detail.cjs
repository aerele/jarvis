// Answers get_day_detail / get_bucket_detail / get_upcoming from the golden reply files.
// An exact golden match is served as it is; any other request is cut from the same rows
// (window rows for a date, the "now" rows for the obligation buckets), so figures agree.
const toCents = (text) => {
  const [whole, frac = ""] = String(text).split(".");
  return (
    BigInt(whole) * 100n +
    BigInt((frac + "00").slice(0, 2)) * (whole.startsWith("-") ? -1n : 1n)
  );
};
const fromCents = (value) => {
  const abs = value < 0n ? -value : value;
  return `${value < 0n ? "-" : ""}${abs / 100n}.${String(abs % 100n).padStart(
    2,
    "0"
  )}`;
};

const BUCKET_ROWS = {
  today: (row, anchor) => row.due_date === anchor,
  overdue: (row, anchor) => !!row.due_date && row.due_date < anchor,
  undated: (row) => !row.due_date,
  immediate: (row, anchor) => !row.due_date || row.due_date < anchor,
  now: () => true,
};

class DetailReplies {
  constructor(golden) {
    this.golden = golden;
  }

  // args: the request body; snapshot: { anchor, month }. Returns the reply data, or null for refused.
  detail(args, snapshot) {
    const wanted = this.#wanted(args);
    const exact = this.golden
      .replyList()
      .find((item) => this.#same(item.request, wanted));
    if (exact && exact.data.total <= (args.page_size || 25)) return exact.data;
    const rows = this.#sorted(this.#rows(wanted, snapshot), args);
    return rows ? this.#page(rows, args) : null;
  }

  upcoming(args) {
    const rows = this.#window("Payable").filter(
      (row) => row.due_date > args.date
    );
    const byDate = new Map();
    for (const row of rows) {
      const day = byDate.get(row.due_date) || {
        date: row.due_date,
        remaining: 0n,
        rows: 0,
      };
      day.remaining += toCents(row.remaining);
      day.rows += 1;
      byDate.set(row.due_date, day);
    }
    const list = [...byDate.values()]
      .sort((a, b) => a.date.localeCompare(b.date))
      .map((day) => ({ ...day, remaining: fromCents(day.remaining) }));
    return { rows: list, total: list.length };
  }

  #wanted(args) {
    const kind = args.date ? "day" : "bucket";
    return {
      kind,
      side: args.side,
      sort: args.sort ?? null,
      order: args.order ?? null,
      ...(kind === "day" ? { date: args.date } : { bucket: args.bucket }),
    };
  }

  #same(request, wanted) {
    return ["kind", "side", "sort", "order", "date", "bucket"].every(
      (key) => (request[key] ?? null) === (wanted[key] ?? null)
    );
  }

  #window(side) {
    return (
      this.golden.reply(`default_bucket_window_${side.toLowerCase()}`)?.rows ||
      []
    );
  }

  #rows(wanted, snapshot) {
    if (wanted.kind === "day")
      return this.#window(wanted.side).filter(
        (row) => row.due_date === wanted.date
      );
    if (wanted.bucket === "window") return this.#window(wanted.side);
    if (wanted.side !== "Payable" || !BUCKET_ROWS[wanted.bucket]) return null;
    const now = this.golden.reply("default_bucket_now")?.rows || [];
    return now.filter((row) =>
      BUCKET_ROWS[wanted.bucket](row, snapshot.anchor)
    );
  }

  #sorted(rows, args) {
    if (!rows) return null;
    const key = {
      remaining: (r) => toCents(r.remaining),
      due_date: (r) => r.due_date || "9999",
      party: (r) => (r.party_name || r.party).toLowerCase(),
    }[args.sort];
    if (!key) return rows;
    const direction = args.order === "desc" ? -1 : 1;
    return [...rows].sort(
      (a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0) * direction
    );
  }

  #page(rows, args) {
    const size = args.page_size || 25;
    const page = args.page || 1;
    const slice = rows.slice((page - 1) * size, page * size);
    const parties = this.#parties(rows);
    return {
      rows: slice,
      parties,
      total: rows.length,
      party_total: parties.length,
      total_remaining: fromCents(
        rows.reduce((sum, row) => sum + toCents(row.remaining), 0n)
      ),
      page,
      party_page: 1,
      page_size: size,
      has_more: page * size < rows.length,
      generated_at: this.golden.reply("default_bucket_now")?.generated_at,
    };
  }

  #parties(rows) {
    const byParty = new Map();
    for (const row of rows) {
      const item = byParty.get(row.party) || {
        party: row.party,
        party_name: row.party_name,
        party_type: row.party_type,
        remaining: 0n,
        terms: 0,
        invoices: 0,
      };
      item.remaining += toCents(row.remaining);
      item.terms += 1;
      item.invoices += 1;
      byParty.set(row.party, item);
    }
    return [...byParty.values()].map((item) => ({
      ...item,
      remaining: fromCents(item.remaining),
    }));
  }
}

module.exports = { DetailReplies };
