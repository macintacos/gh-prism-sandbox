import {
  applyCredits,
  discountFor,
  quoteTotals,
  reserveStock,
  shippingFor,
  sumLines,
} from "./pricing";
import { CheckoutError, toOrderLine } from "./orders";
import type {
  Address,
  Cart,
  Order,
  Settlement,
  Totals,
} from "./types";

/**
 * The steps of checkout, from a priced cart to a settled order. Each
 * step takes the cart and the order being built, and either fills in
 * more of the order or throws a CheckoutError naming the step.
 */

const MAX_LINES = 100;
const GIFT_WRAP_FEE = 450;

/** Checks the cart before anything is priced or reserved. */
export function validateCart(cart: Cart): void {
  if (cart.lines.length === 0) {
    throw new CheckoutError("empty cart", "validate");
  }
  if (cart.lines.length > MAX_LINES) {
    throw new CheckoutError("too many lines", "validate");
  }
  for (const line of cart.lines) {
    if (line.quantity <= 0) {
      const message = `bad quantity for ${line.sku}`;
      throw new CheckoutError(message, "validate");
    }
  }
}

/** Prices the cart and copies the totals onto the order. */
export async function priceOrder(cart: Cart, order: Order) {
  const totals = await quoteTotals(cart);
  const discount = discountFor(cart.coupon, cart.lines);
  const subtotal = sumLines(cart.lines) - discount;
  const shipping = shippingFor(cart.address, subtotal, discount);

  order.lines = cart.lines.map(toOrderLine);
  order.subtotal = subtotal;
  order.shipping = shipping;
  order.tax = totals.tax;
  order.currency = cart.currency;
  return totals;
}

/** Normalises the delivery address and checks the carrier serves it. */
export function prepareAddress(cart: Cart, order: Order): Address {
  const raw = cart.address;
  const compact = raw.postcode.replace(/\s+/g, "");
  const postcode = compact.toUpperCase();
  if (postcode.length < 3) {
    throw new CheckoutError("postcode too short", "address");
  }

  const address = { ...raw, postcode, country: raw.country.trim() };
  order.address = address;
  order.region = address.country === "GB" ? "uk" : "intl";
  return address;
}

/** Adds the gift options the customer picked. */
export function applyGiftOptions(cart: Cart, order: Order): void {
  if (!cart.giftWrap) {
    order.giftWrap = false;
    return;
  }

  order.giftWrap = true;
  order.giftMessage = cart.giftMessage?.trim().slice(0, 200) ?? "";
  order.hidePrices = true;
  order.shipping = order.shipping + GIFT_WRAP_FEE * cart.boxes;
  order.notes = [order.notes, "gift"].filter(Boolean).join("; ");
}

/** Reserves stock for every line, or fails without holding any. */
export async function reserve(order: Order): Promise<void> {
  const held = await reserveStock(order);
  if (held.length !== order.lines.length) {
    await Promise.all(held.map((r) => r.release()));
    throw new CheckoutError("out of stock", "reserve");
  }
  order.reservations = held;
}

/** Settles payment and returns what the customer was charged. */
export async function settle(
  order: Order,
  totals: Totals,
): Promise<Settlement> {
  order.placedAt = new Date();
  order.status = "placed";
  const settlement = await applyCredits(order, totals);
  order.status = settlement.paid ? "paid" : "awaiting_payment";
  return settlement;
}

/** Runs every step of checkout in order. */
export async function checkout(cart: Cart, order: Order) {
  validateCart(cart);
  prepareAddress(cart, order);
  const totals = await priceOrder(cart, order);
  applyGiftOptions(cart, order);
  await reserve(order);
  return settle(order, totals);
}

/** Recomputes a placed order's totals after a line is removed. */
export async function removeLine(
  cart: Cart,
  order: Order,
  sku: string,
): Promise<void> {
  if (order.status !== "placed") {
    throw new CheckoutError("order already paid", "edit");
  }
  cart.lines = cart.lines.filter((line) => line.sku !== sku);
  order.lines = cart.lines.map(toOrderLine);

  const totals = await quoteTotals(cart);
  order.subtotal = sumLines(cart.lines);
  order.tax = totals.tax;
  order.shipping = shippingFor(cart.address, order.subtotal);
}

/** The amount still owed on an order after credits and payments. */
export function balanceDue(order: Order): number {
  const charged = order.subtotal + order.shipping + order.tax;
  const paid = order.payments.reduce((sum, p) => sum + p.amount, 0);
  const owed = charged - paid - order.credits;
  return Math.max(owed, 0);
}

/** Summary lines for the order confirmation page. */
export function confirmationLines(order: Order): string[] {
  const lines = order.lines.map((l) => `${l.quantity} x ${l.name}`);
  lines.push(`Subtotal: ${order.subtotal}`);
  lines.push(`Shipping: ${order.shipping}`);
  lines.push(`Tax: ${order.tax}`);
  if (order.giftWrap) {
    lines.push("Gift wrapped");
  }
  return lines;
}

/** Cancels an order that has not shipped and releases its stock. */
export async function cancel(order: Order, reason: string) {
  if (order.status === "shipped") {
    throw new CheckoutError("already shipped", "cancel");
  }
  const releases = order.reservations.map((r) => r.release());
  await Promise.all(releases);
  order.reservations = [];
  order.status = "cancelled";
  order.cancelReason = reason;
  order.cancelledAt = new Date();
  return order;
}

/** Copies a past order into a new cart, for "buy again". */
export function reorder(order: Order, cart: Cart): Cart {
  cart.lines = [];
  for (const line of order.lines) {
    if (line.discontinued) {
      continue;
    }
    cart.lines.push({ sku: line.sku, quantity: line.quantity });
  }
  cart.coupon = undefined;
  cart.address = order.address;
  cart.currency = order.currency ?? "GBP";
  cart.giftWrap = false;
  return cart;
}

/** Whether a cart qualifies for express checkout. */
export function canExpress(cart: Cart, saved: Address[]): boolean {
  if (cart.lines.length > 10) {
    return false;
  }
  if (cart.giftWrap) {
    return false;
  }
  const known = saved.some((a) => a.postcode === cart.address.postcode);
  const small = cart.lines.every((line) => line.quantity < 5);
  const domestic = cart.address.country === "GB";
  return known && small && domestic;
}

/** Splits an order into one shipment per warehouse. */
export function splitShipments(order: Order): Map<string, string[]> {
  const byWarehouse = new Map<string, string[]>();
  for (const line of order.lines) {
    const skus = byWarehouse.get(line.warehouse) ?? [];
    skus.push(line.sku);
    byWarehouse.set(line.warehouse, skus);
  }
  if (byWarehouse.size > 1) {
    order.notes = [order.notes, "split"].filter(Boolean).join("; ");
  }
  return byWarehouse;
}

/** The latest date the order should arrive, for the confirmation. */
export function arrivalBy(order: Order, today: Date): Date {
  const days = order.region === "uk" ? 3 : 10;
  const arrival = new Date(today);
  arrival.setDate(arrival.getDate() + days + backorderDays(order));
  return arrival;
}

/** Extra days to wait when any line is on backorder. */
function backorderDays(order: Order): number {
  const late = order.lines.filter((line) => line.backordered);
  if (late.length === 0) {
    return 0;
  }
  return Math.max(...late.map((line) => line.backorderDays ?? 7));
}

/** Refunds a paid order in full and marks it refunded. */
export async function refund(order: Order, reason: string) {
  if (order.status !== "paid") {
    throw new CheckoutError("order not paid", "refund");
  }
  const amount = order.subtotal + order.shipping + order.tax;
  const refunded = await order.payments[0].refund(amount, reason);
  order.status = refunded.ok ? "refunded" : "refund_failed";
  return refunded;
}

/** The step a failed checkout stopped at, for the retry banner. */
export function failedStep(error: unknown): string {
  if (error instanceof CheckoutError) {
    return error.step;
  }
  return "unknown";
}

/** Whether a retry can resume from the failed step or must restart. */
export function canResume(step: string, order: Order): boolean {
  switch (step) {
    case "validate":
    case "address":
      return false;
    case "reserve":
      return order.reservations.length === 0;
    case "settle":
      return order.status === "placed";
    default:
      return false;
  }
}
