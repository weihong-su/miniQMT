import bigqmt_signal_trader.xtquant_compat as _compat


def __getattr__(name):
    return getattr(_compat.xtdata, name)


def get_full_tick(code_list):
    return _compat.xtdata.get_full_tick(code_list)


def get_market_data(field_list=[], stock_list=[], period="1d", start_time="", end_time="", count=-1, dividend_type="none", fill_data=True):
    return _compat.xtdata.get_market_data(field_list, stock_list, period, start_time, end_time, count, dividend_type, fill_data)


def get_market_data_ex(field_list=[], stock_list=[], period="1d", start_time="", end_time="", count=-1, dividend_type="none", fill_data=True):
    return _compat.xtdata.get_market_data_ex(field_list, stock_list, period, start_time, end_time, count, dividend_type, fill_data)


def get_local_data(field_list=[], stock_list=[], period="1d", start_time="", end_time="", count=-1, dividend_type="none", fill_data=True, data_dir=None):
    return _compat.xtdata.get_local_data(field_list, stock_list, period, start_time, end_time, count, dividend_type, fill_data, data_dir)


def get_instrument_detail(stock_code):
    return _compat.xtdata.get_instrument_detail(stock_code)


def get_instrumentdetail(stock_code):
    return _compat.xtdata.get_instrumentdetail(stock_code)


def get_instrument_type(stock_code, variety_list=None):
    return _compat.xtdata.get_instrument_type(stock_code, variety_list)


def get_stock_list_in_sector(sector_name, real_timetag=-1):
    return _compat.xtdata.get_stock_list_in_sector(sector_name, real_timetag=real_timetag)


def get_sector_list():
    return _compat.xtdata.get_sector_list()


def get_sector_info(sector_name=""):
    return _compat.xtdata.get_sector_info(sector_name)


def subscribe_quote(stock_code, period="1d", start_time="", end_time="", count=0, callback=None):
    return _compat.xtdata.subscribe_quote(stock_code, period, start_time, end_time, count, callback)


def subscribe_quote2(stock_code, period="1d", start_time="", end_time="", count=0, dividend_type=None, callback=None):
    return _compat.xtdata.subscribe_quote2(stock_code, period, start_time, end_time, count, dividend_type, callback)


def subscribe_whole_quote(code_list, callback=None):
    return _compat.xtdata.subscribe_whole_quote(code_list, callback=callback)


def unsubscribe_quote(seq):
    return _compat.xtdata.unsubscribe_quote(seq)


def run():
    return _compat.xtdata.run()


def get_divid_factors(stock_code, start_time="", end_time=""):
    return _compat.xtdata.get_divid_factors(stock_code, start_time, end_time)


def getDividFactors(*args, **kwargs):
    return _compat.xtdata.get_divid_factors(*args, **kwargs)


def download_history_data(stock_code, period, start_time="", end_time="", incrementally=None):
    return _compat.xtdata.download_history_data(stock_code, period, start_time, end_time, incrementally)


def download_history_data2(stock_list, period, start_time="", end_time="", callback=None, incrementally=None):
    return _compat.xtdata.download_history_data2(stock_list, period, start_time, end_time, callback, incrementally)


def get_trading_dates(market, start_time="", end_time="", count=-1):
    return _compat.xtdata.get_trading_dates(market, start_time, end_time, count)


def get_holidays():
    return _compat.xtdata.get_holidays()


def download_holiday_data(incrementally=True):
    return _compat.xtdata.download_holiday_data(incrementally)


def get_ipo_info(start_time="", end_time=""):
    return _compat.xtdata.get_ipo_info(start_time, end_time)


def get_etf_info():
    return _compat.xtdata.get_etf_info()


def download_etf_info():
    return _compat.xtdata.download_etf_info()


def get_option_list(undl_code, dedate, opttype="", isavailavle=False):
    return _compat.xtdata.get_option_list(undl_code, dedate, opttype, isavailavle)


def get_his_option_list(undl_code, dedate):
    return _compat.xtdata.get_his_option_list(undl_code, dedate)


def get_his_option_list_batch(undl_code, start_time="", end_time=""):
    return _compat.xtdata.get_his_option_list_batch(undl_code, start_time, end_time)


def get_financial_data(stock_list, table_list=[], start_time="", end_time="", report_type="report_time"):
    return _compat.xtdata.get_financial_data(stock_list, table_list, start_time, end_time, report_type)


def download_financial_data(stock_list, table_list=[], start_time="", end_time="", incrementally=None):
    return _compat.xtdata.download_financial_data(stock_list, table_list, start_time, end_time, incrementally)


def download_financial_data2(stock_list, table_list=[], start_time="", end_time="", callback=None):
    return _compat.xtdata.download_financial_data2(stock_list, table_list, start_time, end_time, callback)


def call_formula(formula_name, stock_code, period, start_time="", end_time="", count=-1, dividend_type=None, extend_param={}):
    return _compat.xtdata.call_formula(formula_name, stock_code, period, start_time, end_time, count, dividend_type, extend_param)


def subscribe_formula(formula_name, stock_code, period, start_time="", end_time="", count=-1, dividend_type=None, extend_param={}, callback=None):
    return _compat.xtdata.subscribe_formula(formula_name, stock_code, period, start_time, end_time, count, dividend_type, extend_param, callback)


def unsubscribe_formula(request_id):
    return _compat.xtdata.unsubscribe_formula(request_id)


def get_formula_result(request_id, start_time="", end_time="", count=-1, timeout_second=-1):
    return _compat.xtdata.get_formula_result(request_id, start_time, end_time, count, timeout_second)


def gen_factor_index(data_name, formula_name, vars, sector_list, start_time="", end_time="", period="1d", dividend_type="none"):
    return _compat.xtdata.gen_factor_index(data_name, formula_name, vars, sector_list, start_time, end_time, period, dividend_type)
