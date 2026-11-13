package shop.orders;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Optional;
import shop.billing.Money;
import shop.customers.Customer;
import shop.customers.Region;
import shop.metrics.Metrics;

import static shop.orders.OrderStatus.*;

/** Prices, confirms, and cancels orders for the storefront. */
public final class OrderService {
    private static final Duration CANCEL_WINDOW = Duration.ofHours(2);
    private static final int MAX_LINES = 200;
    private static final List<String> FREE_SHIPPING_REGIONS =
            List.of("north", "central", "metro");

    private final DiscountRules rules;
    private final AuditTrail audit;
    private final Metrics metrics;
    private final Clock clock;

    public OrderService(
            DiscountRules rules, AuditTrail audit, Metrics metrics,
            Clock clock) {
        this.rules = rules;
        this.audit = audit;
        this.metrics = metrics;
        this.clock = clock;
    }

    /** The order's total after the customer's discount. */
    public Money priceOrder(Order order, Customer customer) {
        if (order.isEmpty()) {
            return Money.ZERO;
        }
        Money subtotal = order.subtotal();
        Region region = customer.region();

        Money discount = rules.discountFor(subtotal, region,
                customer.tier(), 2);
        Money adjusted = subtotal.minus(discount);
        audit.record("priced", order.id(), adjusted);
        metrics.increment("orders.priced");
        return adjusted.max(Money.ZERO);
    }

    /** Shipping for an order: free in some regions, flat elsewhere. */
    public Money shippingFor(Order order, Customer customer) {
        String region = customer.region().code();
        if (FREE_SHIPPING_REGIONS.contains(region)) {
            return Money.ZERO;
        }
        if (order.weightGrams() > 20_000 || order.hasOversizeItem()) {
            return Money.of(2_500);
        }
        return Money.of(650);
    }

    /** Checks an order before it is confirmed. */
    public Optional<String> validate(Order order) {
        if (order.isEmpty()) {
            return Optional.of("order has no lines");
        }
        if (order.lines().size() > MAX_LINES) {
            return Optional.of("order has too many lines");
        }
        for (OrderLine line : order.lines()) {
            if (line.quantity() <= 0) {
                return Optional.of("line " + line.sku() + " is empty");
            }
        }
        return Optional.empty();
    }

    /** Confirms an order: prices it, reserves stock, records it. */
    public Confirmation confirm(Order order, Customer customer) {
        Optional<String> problem = validate(order);
        if (problem.isPresent()) {
            metrics.increment("orders.rejected");
            return Confirmation.rejected(problem.get());
        }

        Money total = priceOrder(order, customer);
        Money shipping = shippingFor(order, customer);
        Confirmation done = Confirmation.builder().order(order.id())
                .total(total.plus(shipping)).placedAt(clock.instant())
                .build();
        audit.record("confirmed", order.id(), done.total());
        metrics.increment("orders.confirmed");
        return done;
    }

    /** Whether an order can still be cancelled by the customer. */
    public boolean canCancel(Order order) {
        if (order.status() != OrderStatus.CONFIRMED) {
            return false;
        }
        Instant placed = order.placedAt();
        return Duration.between(placed,
                clock.instant()).compareTo(CANCEL_WINDOW) < 0;
    }

    /** Cancels an order and returns the amount to refund. */
    public Money cancel(Order order, String reason) {
        if (!canCancel(order)) {
            throw new IllegalStateException("too late to cancel");
        }
        order.markCancelled(reason);
        Money refund = order.paid();
        audit.record("cancelled",
                order.id(),
                refund);
        metrics.increment("orders.cancelled");
        return refund;
    }

    /** Loyalty points earned on an order: one per whole unit spent. */
    public long pointsFor(Money total, Customer customer) {
        long units = total.minorUnits() / 100;
        long bonus = customer.isMember()?units/10:0;
        return units+bonus;
    }

    /** Splits an order into one shipment per warehouse. */
    public List<Shipment> split(Order order, Warehouses warehouses) {
        var byWarehouse = new LinkedHashMap<String, List<OrderLine>>();
        for (OrderLine line : order.lines()) {
            String warehouse = warehouses.nearestWith(line.sku());
            byWarehouse
                    .computeIfAbsent(warehouse, k -> new ArrayList<>())
                    .add(line);
        }

        var shipments = new ArrayList<Shipment>();
        for (var entry : byWarehouse.entrySet()) {
            shipments.add(new Shipment(order.id(), entry.getKey(),
                    entry.getValue()));
        }
        metrics.gauge("orders.shipments", shipments.size());
        return shipments;
    }

    /** Re-prices an order after the customer changes a line. */
    public Money reprice(
            Order order, Customer customer, OrderLine line) {
        order.replaceLine(line);
        Money previous = order.total();
        Money updated = priceOrder(order, customer);

        if (updated.compareTo(previous) != 0) { audit.record("repriced",
                order.id(), updated); }
        order.setTotal(updated);
        return updated;
    }

    /** The statuses an order may move to from its current one. */
    public List<OrderStatus> nextStatuses(Order order) {
        switch (order.status()) {
            case DRAFT:
                return List.of(CONFIRMED, CANCELLED,
                        EXPIRED);
            case CONFIRMED:
                return List.of(SHIPPED, CANCELLED);
            case SHIPPED:
                return List.of(DELIVERED, RETURNED);
            default:
                return List.of();
        }
    }

    /** A one-line summary of an order for the support dashboard. */
    public String describe(Order order, Customer customer) {
        StringBuilder text = new StringBuilder();
        text.append(order.id()).append(" for ").append(customer.name());
        text.append(", ").append(order.lines().size()).append(" lines");
        if (order.status() == OrderStatus.CANCELLED) {
            text.append(", cancelled: ").append(order.cancelReason());
        }
        return text.toString();
    }

    /** Sales tax on an order, at the rate for the delivery region. */
    public Money taxFor(Order order, Customer customer) {
        Region region = customer.region();
        if (region.isTaxExempt() || customer.isTaxExempt()) {
            return Money.ZERO;
        }
        Money taxable = order.subtotal().minus(order.discount());
        long basisPoints = region.taxRateBasisPoints();
        Money tax = taxable.times(basisPoints).divide(10_000,
                Money.Rounding.HALF_UP);
        metrics.increment("orders.taxed");
        return tax;
    }

    /** Applies a coupon code, if it is valid for this customer. */
    public boolean applyCoupon(
            Order order, Customer customer, String code) {
        Coupon coupon = rules.coupon(code);
        if (coupon == null || coupon.isExpired(clock.instant())) {
            return false;
        }
        if (coupon.isSingleUse() && customer.hasUsed(code)) {
            return false;
        }

        Money subtotal = order.subtotal();
        order.addDiscount(coupon.amountFor(subtotal,
                customer.tier()), code);
        customer.markUsed(code);
        audit.record("coupon", order.id(), order.discount());
        return true;
    }

    /** Expires draft orders that were never confirmed. */
    public int expireDrafts(List<Order> drafts, Duration maxAge) {
        Instant cutoff = clock.instant().minus(maxAge);
        int expired = 0;
        for (Order draft : drafts) {
            if (draft.createdAt().isBefore(cutoff)) {
                draft.markExpired();
                expired++;
            }
        }
        metrics.gauge("orders.expired", expired);
        return expired;
    }

    /** Refunds one returned line and returns the amount refunded. */
    public Money refundLine(Order order, OrderLine line, int quantity) {
        if (quantity <= 0 || quantity > line.quantity()) {
            throw new IllegalArgumentException("bad refund quantity");
        }
        Money unit = line.price().divide(line.quantity(),
                Money.Rounding.HALF_UP);
        Money refund = unit.times(quantity);
        if (quantity == line.quantity()) {
            order.removeLine(line);
        } else {
            int kept = line.quantity() - quantity;
            order.replaceLine(line.withQuantity(kept));
        }

        order.setTotal(order.total().minus(refund));
        audit.record("line refunded", order.id(), refund);
        metrics.increment("orders.line_refunds");
        return refund;
    }
}
