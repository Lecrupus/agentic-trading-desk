/**
 * exchange.hpp - the order-book simulator core.
 *
 * These classes come from the original Cryptocurrency Trading Platform
 * (main.cpp). The menu loop is gone: instead, an Exchange object owns the
 * order book, the wallet and the clock, so another program (engine_server.cpp,
 * and through it the MCP server and the agents) can drive the simulation.
 *
 * Nothing in this file writes to stdout. The server uses stdout as its
 * protocol channel, so a stray print here would corrupt a response.
 */
#pragma once

#include <algorithm>
#include <cmath>
#include <fstream>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

// ==========================================
// 1. Data Structures & Enums
// ==========================================

enum class OrderBookType { bid, ask, unknown, asksale, bidsale };

inline std::string orderTypeToString(OrderBookType t) {
    switch (t) {
        case OrderBookType::bid:     return "bid";
        case OrderBookType::ask:     return "ask";
        case OrderBookType::asksale: return "asksale";
        case OrderBookType::bidsale: return "bidsale";
        default:                     return "unknown";
    }
}

class OrderBookEntry {
public:
    double price;
    double amount;
    std::string timestamp;
    std::string product;
    OrderBookType orderType;
    std::string username;
    int id;   // 0 for historical orders; >0 for orders the agent placed

    OrderBookEntry(double _price, double _amount, std::string _timestamp,
                   std::string _product, OrderBookType _orderType,
                   std::string _username = "dataset", int _id = 0)
    : price(_price), amount(_amount), timestamp(_timestamp),
      product(_product), orderType(_orderType), username(_username), id(_id) {}

    static OrderBookType stringToOrderBookType(const std::string& s) {
        if (s == "ask") return OrderBookType::ask;
        if (s == "bid") return OrderBookType::bid;
        return OrderBookType::unknown;
    }

    static bool compareByTimestamp(const OrderBookEntry& e1, const OrderBookEntry& e2) {
        return e1.timestamp < e2.timestamp;
    }

    static bool compareByPriceAsc(const OrderBookEntry& e1, const OrderBookEntry& e2) {
        return e1.price < e2.price;
    }

    static bool compareByPriceDesc(const OrderBookEntry& e1, const OrderBookEntry& e2) {
        return e1.price > e2.price;
    }
};

// ==========================================
// 2. CSV / String Parsing Utilities
// ==========================================

class CSVReader {
public:
    static std::vector<std::string> tokenise(std::string csvLine, char separator) {
        std::vector<std::string> tokens;
        std::string::size_type start, end;
        start = csvLine.find_first_not_of(separator, 0);
        if (start == std::string::npos) return tokens;
        do {
            end = csvLine.find_first_of(separator, start);
            if (start == csvLine.length() || start == end) break;
            if (end != std::string::npos) tokens.push_back(csvLine.substr(start, end - start));
            else tokens.push_back(csvLine.substr(start));
            start = end + 1;
        } while (end != std::string::npos);
        return tokens;
    }

    // Turns one line of CSV into an OrderBookEntry. Throws if the line is malformed.
    static OrderBookEntry stringsToOBE(std::vector<std::string> tokens) {
        if (tokens.size() != 5) throw std::invalid_argument("wrong number of columns");
        OrderBookType type = OrderBookEntry::stringToOrderBookType(tokens[2]);
        if (type == OrderBookType::unknown) throw std::invalid_argument("unknown order type");
        double price = std::stod(tokens[3]);
        double amount = std::stod(tokens[4]);
        if (price <= 0 || amount <= 0) throw std::invalid_argument("non-positive price or amount");
        return OrderBookEntry(price, amount, tokens[0], tokens[1], type);
    }

    // Reads the whole file, skipping any line it cannot parse.
    static std::vector<OrderBookEntry> readCSV(const std::string& csvFilename, int* badLines = nullptr) {
        std::vector<OrderBookEntry> entries;
        std::ifstream csvFile{csvFilename};
        std::string line;
        int bad = 0;
        while (std::getline(csvFile, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();   // CRLF files
            if (line.empty()) continue;
            try {
                entries.push_back(stringsToOBE(tokenise(line, ',')));
            } catch (const std::exception&) {
                ++bad;
            }
        }
        if (badLines) *badLines = bad;
        return entries;
    }
};

// ==========================================
// 3. Wallet Class
// ==========================================

class Wallet {
public:
    void insertCurrency(const std::string& type, double amount) {
        if (amount < 0) throw std::invalid_argument("negative amount");
        currencies[type] += amount;
    }

    bool removeCurrency(const std::string& type, double amount) {
        if (amount < 0 || !containsCurrency(type, amount)) return false;
        currencies[type] -= amount;
        return true;
    }

    bool containsCurrency(const std::string& type, double amount) const {
        auto it = currencies.find(type);
        if (it == currencies.end()) return amount <= 0;
        return it->second >= amount;
    }

    double balance(const std::string& type) const {
        auto it = currencies.find(type);
        return it == currencies.end() ? 0.0 : it->second;
    }

    // Move the funds once a sale has actually been matched.
    void processSale(const OrderBookEntry& sale) {
        std::vector<std::string> currs = CSVReader::tokenise(sale.product, '/');
        if (currs.size() != 2) return;

        if (sale.orderType == OrderBookType::asksale) {   // we sold
            currencies[currs[0]] -= sale.amount;
            currencies[currs[1]] += sale.amount * sale.price;
        }
        if (sale.orderType == OrderBookType::bidsale) {   // we bought
            currencies[currs[0]] += sale.amount;
            currencies[currs[1]] -= sale.amount * sale.price;
        }
    }

    const std::map<std::string, double>& balances() const { return currencies; }

private:
    std::map<std::string, double> currencies;
};

// ==========================================
// 4. OrderBook Class
// ==========================================

class OrderBook {
public:
    explicit OrderBook(std::vector<OrderBookEntry> entries) : orders(std::move(entries)) {
        std::stable_sort(orders.begin(), orders.end(), OrderBookEntry::compareByTimestamp);
    }

    std::vector<std::string> getKnownProducts() const {
        std::set<std::string> products;
        for (const OrderBookEntry& e : orders) products.insert(e.product);
        return {products.begin(), products.end()};
    }

    // Every distinct timestamp, in order. Each one is a "time step".
    std::vector<std::string> getTimestamps() const {
        std::set<std::string> times;
        for (const OrderBookEntry& e : orders) times.insert(e.timestamp);
        return {times.begin(), times.end()};
    }

    std::vector<OrderBookEntry> getOrders(OrderBookType type, const std::string& product,
                                          const std::string& timestamp) const {
        std::vector<OrderBookEntry> sub;
        for (const OrderBookEntry& e : orders) {
            if (e.orderType == type && e.product == product && e.timestamp == timestamp) {
                sub.push_back(e);
            }
        }
        return sub;
    }

    std::vector<OrderBookEntry> getOrdersById(int idAbove = 0) const {
        std::vector<OrderBookEntry> sub;
        for (const OrderBookEntry& e : orders) if (e.id > idAbove) sub.push_back(e);
        return sub;
    }

    void insertOrder(const OrderBookEntry& order) {
        // Keep the vector sorted by timestamp without re-sorting everything.
        auto pos = std::upper_bound(orders.begin(), orders.end(), order,
                                    OrderBookEntry::compareByTimestamp);
        orders.insert(pos, order);
    }

    bool removeOrder(int id) {
        if (id <= 0) return false;   // historical orders cannot be removed
        auto it = std::find_if(orders.begin(), orders.end(),
                               [id](const OrderBookEntry& e) { return e.id == id; });
        if (it == orders.end()) return false;
        orders.erase(it);
        return true;
    }

    void removeAgentOrders() {
        orders.erase(std::remove_if(orders.begin(), orders.end(),
                                    [](const OrderBookEntry& e) { return e.id > 0; }),
                     orders.end());
    }

    /**
     * The matching engine. Cheapest asks are matched against the highest bids;
     * a trade happens when bid >= ask, at the ask price. Each sale carries the
     * id of the agent order involved (0 if two historical orders matched), so
     * the caller can settle the wallet. Two agent orders never trade with
     * each other.
     */
    std::vector<OrderBookEntry> matchAsksToBids(const std::string& product,
                                                const std::string& timestamp) const {
        std::vector<OrderBookEntry> asks = getOrders(OrderBookType::ask, product, timestamp);
        std::vector<OrderBookEntry> bids = getOrders(OrderBookType::bid, product, timestamp);
        std::vector<OrderBookEntry> sales;

        std::stable_sort(asks.begin(), asks.end(), OrderBookEntry::compareByPriceAsc);
        std::stable_sort(bids.begin(), bids.end(), OrderBookEntry::compareByPriceDesc);

        for (OrderBookEntry& ask : asks) {
            for (OrderBookEntry& bid : bids) {
                if (ask.amount <= 0) break;
                if (bid.price < ask.price) break;   // bids are sorted: none further down can match
                if (bid.amount <= 0) continue;
                if (bid.id > 0 && ask.id > 0) continue;

                double traded = std::min(bid.amount, ask.amount);
                OrderBookEntry sale{ask.price, traded, timestamp, product, OrderBookType::asksale};
                if (bid.id > 0) {
                    sale.username = bid.username;
                    sale.orderType = OrderBookType::bidsale;
                    sale.id = bid.id;
                }
                if (ask.id > 0) {
                    sale.username = ask.username;
                    sale.orderType = OrderBookType::asksale;
                    sale.id = ask.id;
                }
                sales.push_back(sale);
                bid.amount -= traded;
                ask.amount -= traded;
            }
        }
        return sales;
    }

private:
    std::vector<OrderBookEntry> orders;
};

// ==========================================
// 5. Exchange - the object the server drives
// ==========================================

struct Fill {
    int orderId;
    std::string product;
    std::string side;   // "bid" (we bought) or "ask" (we sold)
    double price;
    double amount;
};

struct StepResult {
    std::string matchedAt;           // the time step that was just matched
    std::vector<Fill> fills;         // agent fills only
    std::vector<int> expired;        // agent orders that did not fully fill
    bool done;                       // no time steps left
};

class Exchange {
public:
    static constexpr const char* AGENT = "simuser";

    explicit Exchange(std::vector<OrderBookEntry> history,
                      std::map<std::string, double> startingBalances = defaultBalances())
    : book(std::move(history)), starting(std::move(startingBalances)) {
        timestamps = book.getTimestamps();
        if (timestamps.empty()) throw std::invalid_argument("no market data");
        reset();
    }

    static std::map<std::string, double> defaultBalances() {
        return {{"BTC", 10.0}, {"ETH", 100.0}, {"USDT", 100000.0}, {"DOGE", 0.0}};
    }

    // Back to the first time step, with the starting wallet and no agent orders.
    void reset() {
        book.removeAgentOrders();
        wallet = Wallet{};
        for (const auto& [currency, amount] : starting) wallet.insertCurrency(currency, amount);
        stepIndex = 0;
        nextId = 1;
    }

    const std::string& currentTime() const { return timestamps[std::min(stepIndex, lastStep())]; }
    int step() const { return static_cast<int>(stepIndex); }
    int totalSteps() const { return static_cast<int>(timestamps.size()); }
    bool done() const { return stepIndex >= timestamps.size(); }
    std::vector<std::string> products() const { return book.getKnownProducts(); }
    const Wallet& getWallet() const { return wallet; }

    // Book at the current time step, best price first.
    std::vector<OrderBookEntry> bids(const std::string& product) const {
        auto v = book.getOrders(OrderBookType::bid, product, currentTime());
        std::stable_sort(v.begin(), v.end(), OrderBookEntry::compareByPriceDesc);
        return v;
    }

    std::vector<OrderBookEntry> asks(const std::string& product) const {
        auto v = book.getOrders(OrderBookType::ask, product, currentTime());
        std::stable_sort(v.begin(), v.end(), OrderBookEntry::compareByPriceAsc);
        return v;
    }

    // Funds not already promised to an open order.
    double available(const std::string& currency) const {
        double reserved = 0;
        for (const OrderBookEntry& o : openOrders()) {
            auto currs = CSVReader::tokenise(o.product, '/');
            if (o.orderType == OrderBookType::ask && currs[0] == currency) reserved += o.amount;
            if (o.orderType == OrderBookType::bid && currs[1] == currency) reserved += o.amount * o.price;
        }
        return wallet.balance(currency) - reserved;
    }

    /**
     * Places an agent order at the current time step. Returns the new order id,
     * or 0 with `error` set if the order was rejected.
     */
    int placeOrder(OrderBookType side, const std::string& product, double price,
                   double amount, std::string& error) {
        if (done()) { error = "market closed: no time steps left"; return 0; }
        if (side != OrderBookType::bid && side != OrderBookType::ask) { error = "side must be bid or ask"; return 0; }
        auto known = products();
        if (std::find(known.begin(), known.end(), product) == known.end()) { error = "unknown product " + product; return 0; }
        if (!(price > 0) || !std::isfinite(price)) { error = "price must be positive"; return 0; }
        if (!(amount > 0) || !std::isfinite(amount)) { error = "amount must be positive"; return 0; }

        auto currs = CSVReader::tokenise(product, '/');
        const std::string& pays = side == OrderBookType::ask ? currs[0] : currs[1];
        double needed = side == OrderBookType::ask ? amount : amount * price;
        if (available(pays) < needed) { error = "insufficient " + pays; return 0; }

        int id = nextId++;
        book.insertOrder(OrderBookEntry{price, amount, currentTime(), product, side, AGENT, id});
        return id;
    }

    bool cancelOrder(int id) { return book.removeOrder(id); }

    std::vector<OrderBookEntry> openOrders() const { return book.getOrdersById(0); }

    /**
     * Runs the matching engine for every product at the current time step,
     * settles the agent's fills into the wallet, and moves the clock on.
     * Agent orders live for one step: whatever did not fill is erased, so a
     * stale order can never sit in the book and cross a later price.
     */
    StepResult advance() {
        StepResult result{currentTime(), {}, {}, false};
        if (done()) { result.done = true; return result; }

        std::map<int, double> filledAmount;
        for (const std::string& product : products()) {
            for (const OrderBookEntry& sale : book.matchAsksToBids(product, currentTime())) {
                if (sale.id == 0) continue;
                wallet.processSale(sale);
                filledAmount[sale.id] += sale.amount;
                result.fills.push_back({sale.id, product,
                                        sale.orderType == OrderBookType::bidsale ? "bid" : "ask",
                                        sale.price, sale.amount});
            }
        }
        for (const OrderBookEntry& o : openOrders()) {
            if (filledAmount[o.id] + 1e-12 < o.amount) result.expired.push_back(o.id);
        }
        book.removeAgentOrders();

        ++stepIndex;
        result.done = done();
        return result;
    }

private:
    std::size_t lastStep() const { return timestamps.size() - 1; }

    OrderBook book;
    Wallet wallet;
    std::map<std::string, double> starting;
    std::vector<std::string> timestamps;
    std::size_t stepIndex = 0;
    int nextId = 1;
};
