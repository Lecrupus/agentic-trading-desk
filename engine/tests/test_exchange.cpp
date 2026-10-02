/**
 * test_exchange.cpp - a tiny dependency-free test harness for the engine core.
 *
 * Build:  g++ -std=c++17 -O2 -o build/test_exchange engine/tests/test_exchange.cpp
 * Run:    ./build/test_exchange
 *
 * Exits non-zero if any check fails, so CI can use it directly.
 */

#include "../include/exchange.hpp"

#include <iostream>

static int testsRun = 0;
static int testsFailed = 0;

void check(const std::string& name, bool condition) {
    ++testsRun;
    if (condition) {
        std::cout << "  [PASS] " << name << std::endl;
    } else {
        ++testsFailed;
        std::cout << "  [FAIL] " << name << std::endl;
    }
}

bool nearly(double a, double b, double tol = 1e-9) {
    return std::fabs(a - b) < tol;
}

const std::string T1 = "2020/03/17 17:01:24.884492";
const std::string T2 = "2020/03/17 17:01:30.099017";

// Two time steps, one product. At T1 the best ask is 10, best bid is 9.
std::vector<OrderBookEntry> smallMarket() {
    return {
        {9.0, 5.0, T1, "ETH/USDT", OrderBookType::bid},
        {8.0, 5.0, T1, "ETH/USDT", OrderBookType::bid},
        {10.0, 2.0, T1, "ETH/USDT", OrderBookType::ask},
        {11.0, 3.0, T1, "ETH/USDT", OrderBookType::ask},
        {9.5, 1.0, T2, "ETH/USDT", OrderBookType::bid},
        {10.5, 1.0, T2, "ETH/USDT", OrderBookType::ask},
    };
}

void testCSVReader() {
    std::cout << "\nCSVReader" << std::endl;

    std::vector<std::string> t = CSVReader::tokenise("a,b,c", ',');
    check("tokenise splits into 3 tokens", t.size() == 3);
    check("tokenise handles an empty line", CSVReader::tokenise("", ',').empty());

    OrderBookEntry e = CSVReader::stringsToOBE(
        CSVReader::tokenise("2020/03/17 17:01:24.884492,ETH/BTC,bid,0.02187308,7.44564869", ','));
    check("parses price", nearly(e.price, 0.02187308));
    check("parses order type", e.orderType == OrderBookType::bid);

    bool threw = false;
    try { CSVReader::stringsToOBE(CSVReader::tokenise("t,ETH/BTC,hold,1,1", ',')); }
    catch (const std::exception&) { threw = true; }
    check("rejects an unknown order type", threw);

    int bad = -1;
    auto rows = CSVReader::readCSV("engine/data/20200317.csv", &bad);
    check("reads the real snapshot (3,540 orders)", rows.size() == 3540);
    check("skips nothing in the real snapshot", bad == 0);
}

void testMatching() {
    std::cout << "\nOrderBook matching" << std::endl;

    OrderBook book{smallMarket()};
    check("historical book does not cross", book.matchAsksToBids("ETH/USDT", T1).empty());

    book.insertOrder({10.5, 3.0, T1, "ETH/USDT", OrderBookType::bid, "simuser", 1});
    auto sales = book.matchAsksToBids("ETH/USDT", T1);
    check("agent bid at 10.5 fills only the 10.0 ask", sales.size() == 1);
    check("fills at the ask price", nearly(sales[0].price, 10.0));
    check("fills the 2 units available", nearly(sales[0].amount, 2.0));
    check("sale is tagged as our buy", sales[0].orderType == OrderBookType::bidsale && sales[0].id == 1);

    book.insertOrder({8.5, 1.0, T1, "ETH/USDT", OrderBookType::ask, "simuser", 2});
    sales = book.matchAsksToBids("ETH/USDT", T1);
    bool ownTrade = false;
    for (auto& s : sales) if (s.id == 2 && s.orderType == OrderBookType::asksale && nearly(s.amount, 1.0)) ownTrade = true;
    check("agent ask at 8.5 sells into the 9.0 bid", ownTrade);
    bool selfTrade = false;
    for (auto& s : sales) if (s.id == 1 && nearly(s.price, 8.5)) selfTrade = true;
    check("agent orders never trade with each other", !selfTrade);
}

void testExchange() {
    std::cout << "\nExchange" << std::endl;

    Exchange ex{smallMarket(), {{"ETH", 1.0}, {"USDT", 100.0}}};
    check("starts at step 0", ex.step() == 0 && ex.currentTime() == T1);
    check("two time steps", ex.totalSteps() == 2);

    std::string err;
    check("rejects an unknown product", ex.placeOrder(OrderBookType::bid, "XRP/USDT", 1, 1, err) == 0);
    check("rejects a bid it cannot pay for", ex.placeOrder(OrderBookType::bid, "ETH/USDT", 10, 11, err) == 0);
    check("says why", err == "insufficient USDT");

    int id = ex.placeOrder(OrderBookType::bid, "ETH/USDT", 10, 5, err);
    check("accepts a bid it can pay for", id == 1);
    check("reserves the funds", nearly(ex.available("USDT"), 50.0));
    check("cannot spend the reserved funds twice",
          ex.placeOrder(OrderBookType::bid, "ETH/USDT", 10, 6, err) == 0);

    StepResult r = ex.advance();
    check("step fills 2 units", r.fills.size() == 1 && nearly(r.fills[0].amount, 2.0));
    check("the partly filled order expires", r.expired.size() == 1 && r.expired[0] == id);
    check("wallet received the ETH", nearly(ex.getWallet().balance("ETH"), 3.0));
    check("wallet paid 20 USDT", nearly(ex.getWallet().balance("USDT"), 80.0));
    check("no orders left open", ex.openOrders().empty());
    check("clock moved to step 1", ex.step() == 1 && ex.currentTime() == T2);

    id = ex.placeOrder(OrderBookType::ask, "ETH/USDT", 12, 1, err);
    check("cancel removes an order", ex.cancelOrder(id) && ex.openOrders().empty());
    check("cannot cancel a historical order", !ex.cancelOrder(0));

    r = ex.advance();
    check("last step reports done", r.done && ex.done());
    check("no orders once the market is closed",
          ex.placeOrder(OrderBookType::bid, "ETH/USDT", 1, 1, err) == 0);

    ex.reset();
    check("reset restores the wallet", nearly(ex.getWallet().balance("USDT"), 100.0));
    check("reset rewinds the clock", ex.step() == 0 && !ex.done());
}

int main() {
    testCSVReader();
    testMatching();
    testExchange();
    std::cout << "\n" << (testsRun - testsFailed) << "/" << testsRun << " passed" << std::endl;
    return testsFailed == 0 ? 0 : 1;
}
