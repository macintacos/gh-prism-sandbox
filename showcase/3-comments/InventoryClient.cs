using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

namespace Shop.Inventory;
/// <summary>
/// Talks to the inventory service on behalf of checkout: holds
/// stock while a customer pays, and releases or commits it after.
/// </summary>
public sealed class InventoryClient
{
    private const int MaxBatch = 50;
    private static readonly TimeSpan RetryDelay =
        TimeSpan.FromMilliseconds(250);

    private readonly IStockApi api;
    private readonly TimeSpan holdWindow;
    private readonly IClock clock;

    public InventoryClient(IStockApi api, TimeSpan holdWindow,
        IClock clock)
    {
        this.api = api;
        this.holdWindow = holdWindow;
        this.clock = clock;
    }

    /// <summary>
    /// Reserves stock for one order line. The reservation holds for
    /// the configured hold window and is released automatically
    /// when it lapses.
    /// </summary>
    public async Task<Reservation> ReserveAsync(
        string orderId, string sku, int quantity,
        CancellationToken token)
    {
        if (quantity <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(quantity));
        }

        var request = new ReserveRequest(orderId, sku, quantity,
            holdWindow);
        return await api.ReserveAsync(request, token);
    }

    /// <summary>
    /// Reserves every line of an order in one call.
    /// </summary>
    /// <remarks>
    /// Lines are sent in batches of at most <see cref="MaxBatch"/>.
    /// If any batch fails, the batches already reserved are
    /// released before the error is rethrown, so a failed call
    /// holds nothing.
    /// </remarks>
    public async Task<List<Reservation>> ReserveAllAsync(
        string orderId, IReadOnlyList<OrderLine> lines,
        CancellationToken token)
    {
        var held = new List<Reservation>();
        try
        {
            foreach (var batch in lines.Chunk(MaxBatch))
            {
                var requests = batch.Select(line =>
                    new ReserveRequest(orderId, line.Sku,
                        line.Quantity, holdWindow));
                held.AddRange(await api.ReserveManyAsync(requests,
                    token));
            }
        }
        catch
        {
            await ReleaseAllAsync(held, CancellationToken.None);
            throw;
        }
        return held;
    }

    /// <summary>
    /// Releases a reservation before its window ends.
    /// </summary>
    public Task ReleaseAsync(Reservation reservation,
        CancellationToken token)
    {
        return api.ReleaseAsync(reservation.Id, token);
    }

    /// <summary>
    /// Releases each reservation, ignoring failures.
    /// </summary>
    public async Task ReleaseAllAsync(
        IEnumerable<Reservation> reservations, CancellationToken token)
    {
        foreach (var reservation in reservations)
        {
            try
            {
                await api.ReleaseAsync(reservation.Id, token);
            }
            catch (StockApiException)
            {
                // The reservation expires on its own when its
                // window lapses, so a failed release only holds
                // stock a little longer than it should.
            }
        }
    }

    /// <summary>
    /// Whether an order still holds any stock.
    /// </summary>
    public async Task<bool> HoldsStockAsync(string orderId,
        CancellationToken token)
    {
        var held = await api.HeldForOrderAsync(orderId, token);
        return held.Any(r => r.ExpiresAt > clock.UtcNow);
    }

    /// <summary>
    /// Turns a reservation into a sale once payment succeeds. The
    /// stock leaves the available count for good and the hold is
    /// closed.
    /// </summary>
    public async Task CommitAsync(Reservation reservation,
        CancellationToken token)
    {
        if (reservation.ExpiresAt <= clock.UtcNow)
        {
            throw new ReservationExpiredException(reservation.Id);
        }

        for (var attempt = 1; ; attempt++)
        {
            try
            {
                await api.CommitAsync(reservation.Id, token);
                return;
            }
            catch (StockApiException ex) when (ex.IsTransient
                && attempt < 3)
            {
                await Task.Delay(RetryDelay * attempt, token);
            }
        }
    }

    /// <summary>
    /// Extends a reservation that is about to lapse, for example
    /// while a customer completes a bank's payment check.
    /// </summary>
    /// <returns>
    /// The reservation with its new expiry, or the old one
    /// unchanged if the service declined to extend it.
    /// </returns>
    public async Task<Reservation> ExtendAsync(Reservation reservation,
        CancellationToken token)
    {
        var extended = await api.ExtendAsync(reservation.Id,
            holdWindow, token);
        return extended ?? reservation;
    }

    /// <summary>
    /// Counts the stock available to sell for each SKU.
    /// </summary>
    public async Task<IReadOnlyDictionary<string, int>> AvailableAsync(
        IEnumerable<string> skus, CancellationToken token)
    {
        var counts = new Dictionary<string, int>();
        foreach (var batch in skus.Chunk(MaxBatch))
        {
            var levels = await api.LevelsAsync(batch, token);
            foreach (var level in levels)
            {
                counts[level.Sku] = level.Free; // never negative
            }
        }
        return counts;
    }

    /*
     * Restock and the methods after it are used by the warehouse
     * tools, not by checkout. They live here so that both share
     * one retry policy and one place to log calls to the service.
     */

    /// <summary>
    /// Adds received stock to a SKU's on-hand count.
    /// </summary>
    public Task RestockAsync(string sku, int quantity,
        CancellationToken token)
    {
        if (quantity <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(quantity));
        }
        return api.AdjustAsync(sku, quantity, "restock", token);
    }

    /// <summary>
    /// Writes off stock that was damaged, lost, or found short in
    /// a count. The reason is recorded with the adjustment.
    /// </summary>
    public Task WriteOffAsync(string sku, int quantity, string reason,
        CancellationToken token)
    {
        if (quantity <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(quantity));
        }
        return api.AdjustAsync(sku, -quantity, reason, token);
    }

    /// <summary>
    /// Moves stock between two warehouses. The move is booked as a
    /// write-off at the source and a restock at the destination, so
    /// each warehouse's history shows its half of the transfer.
    /// </summary>
    public async Task TransferAsync(string sku, int quantity,
        string from, string to, CancellationToken token)
    {
        await api.AdjustAsync(sku, -quantity, "transfer:" + to,
            token, warehouse: from);
        await api.AdjustAsync(sku, quantity, "transfer:" + from,
            token, warehouse: to);
    }

    /// <summary>
    /// Lists reservations that lapsed without being committed
    /// or released, oldest first, for the nightly stock count.
    /// </summary>
    public async Task<IReadOnlyList<Reservation>> LapsedAsync(
        DateTimeOffset since, CancellationToken token)
    {
        var lapsed = await api.LapsedSinceAsync(since, token);
        return lapsed.OrderBy(r => r.ExpiresAt).ToList();
    }

    /// <summary>
    /// Whether a reservation can still be committed right now.
    /// </summary>
    public bool IsLive(Reservation reservation)
    {
        // A reservation the service has already committed or
        // released keeps its old expiry, so the state has to be
        // checked as well as the time.
        return reservation.ExpiresAt > clock.UtcNow
            && reservation.State == ReservationState.Held;
    }
}
