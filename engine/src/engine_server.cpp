/**
 * engine_server.cpp - drives an Exchange over stdin/stdout.
 *
 * Protocol: one command per line in, one JSON object per line out.
 *
 *   products                               -> {"ok":true,"products":[...]}
 *   time                                   -> {"ok":true,"time":...,"step":0,"total_steps":8,"done":false}
 *   book <product> [depth]                 -> best bids/asks at the current step
 *   wallet                                 -> balances and funds still available
 *   place <bid|ask> <product> <price> <amount>
 *   orders                                 -> open agent orders
 *   cancel <id>
 *   step                                   -> match, settle, advance the clock
 *   reset                                  -> back to step 0, starting wallet
 *   quit
 *
 * Every error is {"ok":false,"error":"..."} and the server keeps running.
 * On start-up it prints a {"ok":true,"event":"ready",...} line.
 *
 * Usage: engine_server [path/to/market.csv]
 */

#include "../include/exchange.hpp"

#include <iomanip>
#include <iostream>
#include <sstream>

// ==========================================
// Tiny JSON writer (we only ever write JSON, never parse it)
// ==========================================

namespace json {

std::string str(const std::string& s) {
    std::ostringstream o;
    o << '"';
    for (char c : s) {
        switch (c) {
            case '"':  o << "\\\""; break;
            case '\\': o << "\\\\"; break;
            case '\n': o << "\\n"; break;
            case '\r': o << "\\r"; break;
            case '\t': o << "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    o << "\\u" << std::hex << std::setw(4) << std::setfill('0') << int(c);
                } else {
                    o << c;
                }
        }
    }
    o << '"';
    return o.str();
}

std::string num(double d) {
    if (!std::isfinite(d)) return "null";
    std::ostringstream o;
    o << std::setprecision(12) << d;
    return o.str();
}

std::string boolean(bool b) { return b ? "true" : "false"; }

template <typename T, typename F>
std::string array(const std::vector<T>& items, F toJson) {
    std::string out = "[";
    for (std::size_t i = 0; i < items.size(); ++i) {
        if (i) out += ",";
        out += toJson(items[i]);
    }
    return out + "]";
}

std::string error(const std::string& message) {
    return "{\"ok\":false,\"error\":" + str(message) + "}";
}

}  // namespace json

// ==========================================
// Command handlers
// ==========================================

std::string orderJson(const OrderBookEntry& o) {
    return "{\"id\":" + std::to_string(o.id) +
           ",\"side\":" + json::str(orderTypeToString(o.orderType)) +
           ",\"product\":" + json::str(o.product) +
           ",\"price\":" + json::num(o.price) +
           ",\"amount\":" + json::num(o.amount) + "}";
}

std::string levelJson(const OrderBookEntry& o) {
    return "[" + json::num(o.price) + "," + json::num(o.amount) + "]";
}

std::string timeJson(const Exchange& ex) {
    return "\"time\":" + json::str(ex.currentTime()) +
           ",\"step\":" + std::to_string(ex.step()) +
           ",\"total_steps\":" + std::to_string(ex.totalSteps()) +
           ",\"done\":" + json::boolean(ex.done());
}

double parseNumber(const std::string& s, const char* what) {
    std::size_t used = 0;
    double v;
    try {
        v = std::stod(s, &used);
    } catch (const std::exception&) {
        throw std::invalid_argument(std::string("could not read ") + what + ": " + s);
    }
    if (used != s.size()) throw std::invalid_argument(std::string("could not read ") + what + ": " + s);
    return v;
}

std::string handle(Exchange& ex, const std::vector<std::string>& args) {
    const std::string& cmd = args[0];

    if (cmd == "products") {
        return "{\"ok\":true,\"products\":" + json::array(ex.products(), json::str) + "}";
    }

    if (cmd == "time") {
        return "{\"ok\":true," + timeJson(ex) + "}";
    }

    if (cmd == "book") {
        if (args.size() < 2) return json::error("usage: book <product> [depth]");
        auto known = ex.products();
        if (std::find(known.begin(), known.end(), args[1]) == known.end()) {
            return json::error("unknown product " + args[1]);
        }
        std::size_t depth = 10;
        if (args.size() >= 3) depth = static_cast<std::size_t>(std::max(1.0, parseNumber(args[2], "depth")));

        auto bids = ex.bids(args[1]);
        auto asks = ex.asks(args[1]);
        std::string bestBid = bids.empty() ? "null" : json::num(bids.front().price);
        std::string bestAsk = asks.empty() ? "null" : json::num(asks.front().price);
        std::string spread = (bids.empty() || asks.empty()) ? "null"
                             : json::num(asks.front().price - bids.front().price);
        if (bids.size() > depth) bids.erase(bids.begin() + depth, bids.end());
        if (asks.size() > depth) asks.erase(asks.begin() + depth, asks.end());

        return "{\"ok\":true,\"product\":" + json::str(args[1]) + "," + timeJson(ex) +
               ",\"best_bid\":" + bestBid + ",\"best_ask\":" + bestAsk + ",\"spread\":" + spread +
               ",\"bids\":" + json::array(bids, levelJson) +
               ",\"asks\":" + json::array(asks, levelJson) + "}";
    }

    if (cmd == "wallet") {
        std::string balances = "{", available = "{";
        bool first = true;
        for (const auto& [currency, amount] : ex.getWallet().balances()) {
            if (!first) { balances += ","; available += ","; }
            first = false;
            balances += json::str(currency) + ":" + json::num(amount);
            available += json::str(currency) + ":" + json::num(ex.available(currency));
        }
        return "{\"ok\":true,\"balances\":" + balances + "},\"available\":" + available + "}}";
    }

    if (cmd == "place") {
        if (args.size() != 5) return json::error("usage: place <bid|ask> <product> <price> <amount>");
        OrderBookType side = OrderBookEntry::stringToOrderBookType(args[1]);
        double price = parseNumber(args[3], "price");
        double amount = parseNumber(args[4], "amount");
        std::string error;
        int id = ex.placeOrder(side, args[2], price, amount, error);
        if (id == 0) return json::error(error);
        return "{\"ok\":true,\"order_id\":" + std::to_string(id) + "," + timeJson(ex) + "}";
    }

    if (cmd == "orders") {
        return "{\"ok\":true,\"orders\":" + json::array(ex.openOrders(), orderJson) + "}";
    }

    if (cmd == "cancel") {
        if (args.size() != 2) return json::error("usage: cancel <id>");
        int id = static_cast<int>(parseNumber(args[1], "order id"));
        if (!ex.cancelOrder(id)) return json::error("no open order with id " + args[1]);
        return "{\"ok\":true,\"cancelled\":" + std::to_string(id) + "}";
    }

    if (cmd == "step") {
        StepResult r = ex.advance();
        auto fillJson = [](const Fill& f) {
            return "{\"order_id\":" + std::to_string(f.orderId) +
                   ",\"product\":" + json::str(f.product) +
                   ",\"side\":" + json::str(f.side) +
                   ",\"price\":" + json::num(f.price) +
                   ",\"amount\":" + json::num(f.amount) + "}";
        };
        auto intJson = [](int i) { return std::to_string(i); };
        return "{\"ok\":true,\"matched_at\":" + json::str(r.matchedAt) +
               ",\"fills\":" + json::array(r.fills, fillJson) +
               ",\"expired\":" + json::array(r.expired, intJson) + "," + timeJson(ex) + "}";
    }

    if (cmd == "reset") {
        ex.reset();
        return "{\"ok\":true," + timeJson(ex) + "}";
    }

    return json::error("unknown command " + cmd);
}

// ==========================================
// Main loop
// ==========================================

int main(int argc, char* argv[]) {
    std::ios::sync_with_stdio(false);
    std::string path = argc > 1 ? argv[1] : "engine/data/20200317.csv";

    int badLines = 0;
    std::vector<OrderBookEntry> history = CSVReader::readCSV(path, &badLines);
    if (history.empty()) {
        std::cout << json::error("no market data could be read from " + path) << std::endl;
        return 1;
    }
    std::size_t loaded = history.size();
    Exchange ex{std::move(history)};

    std::cout << "{\"ok\":true,\"event\":\"ready\",\"orders_loaded\":" << loaded
              << ",\"bad_lines\":" << badLines << "," << timeJson(ex) << "}" << std::endl;

    std::string line;
    while (std::getline(std::cin, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        std::istringstream words(line);
        std::vector<std::string> args;
        for (std::string w; words >> w;) args.push_back(w);
        if (args.empty()) continue;
        if (args[0] == "quit") break;

        std::string reply;
        try {
            reply = handle(ex, args);
        } catch (const std::exception& e) {
            reply = json::error(e.what());
        }
        // endl flushes: the client is waiting on this exact line.
        std::cout << reply << std::endl;
    }
    return 0;
}
