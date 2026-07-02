from app.adapters.freqtrade.market_data_catalog import MarketDataCatalog
from app.adapters.freqtrade.market_data_index import FreqtradeMarketDataIndex


def test_indexes_supported_market_data_files(tmp_path) -> None:
    exchange_dir = tmp_path / "okx"
    nested_dir = exchange_dir / "futures"
    nested_dir.mkdir(parents=True)
    spot_file = exchange_dir / "ETH_USDT-1h.feather"
    futures_file = nested_dir / "BTC_USDT_USDT-15m-futures.feather"
    ignored_file = exchange_dir / "README.txt"
    spot_file.write_bytes(b"spot")
    futures_file.write_bytes(b"futures")
    ignored_file.write_text("ignored")

    files = FreqtradeMarketDataIndex(market_data_dir=tmp_path).list_files()

    assert [(item.exchange, item.pair, item.timeframe, item.data_format) for item in files] == [
        ("okx", "ETH/USDT", "1h", "feather"),
        ("okx", "BTC/USDT:USDT", "15m", "feather"),
    ]
    assert files[0].relative_path.as_posix() == "okx/ETH_USDT-1h.feather"
    assert files[0].file_size_bytes == 4


def test_filters_by_exchange_and_missing_directory(tmp_path) -> None:
    okx_dir = tmp_path / "okx"
    binance_dir = tmp_path / "binance"
    okx_dir.mkdir()
    binance_dir.mkdir()
    okx_dir.joinpath("BTC_USDT-5m.json.gz").write_text("{}")
    binance_dir.joinpath("ETH_USDT-1h.parquet").write_text("data")

    files = FreqtradeMarketDataIndex(market_data_dir=tmp_path).list_files(exchange="okx")

    assert len(files) == 1
    assert files[0].exchange == "okx"
    assert files[0].data_format == "json.gz"
    assert FreqtradeMarketDataIndex(market_data_dir=tmp_path / "missing").list_files() == []


def test_skips_unparseable_filenames(tmp_path) -> None:
    exchange_dir = tmp_path / "okx"
    exchange_dir.mkdir()
    exchange_dir.joinpath("BTC_USDT.feather").write_bytes(b"missing timeframe")

    assert FreqtradeMarketDataIndex(market_data_dir=tmp_path).list_files() == []


def test_market_data_catalog_reports_missing_empty_directory(tmp_path) -> None:
    report = MarketDataCatalog(market_data_dir=tmp_path).inspect()

    assert report.status == "missing"
    assert report.entries == []
    assert report.blockers == [f"no exchange directories found under {tmp_path}"]


def test_market_data_catalog_reports_available_files_with_metadata(tmp_path) -> None:
    exchange_dir = tmp_path / "okx"
    exchange_dir.mkdir()
    market_file = exchange_dir / "BTC_USDT-5m-20240101-20240201.json.gz"
    market_file.write_text("{}")

    report = MarketDataCatalog(market_data_dir=tmp_path).inspect()

    assert report.status == "available"
    assert report.blockers == []
    assert len(report.available_entries) == 1
    entry = report.available_entries[0]
    assert entry.status == "available"
    assert entry.exchange == "okx"
    assert entry.pair == "BTC/USDT"
    assert entry.timeframe == "5m"
    assert entry.timerange == "20240101-20240201"
    assert entry.relative_path.as_posix() == "okx/BTC_USDT-5m-20240101-20240201.json.gz"


def test_market_data_catalog_reports_unsupported_files(tmp_path) -> None:
    exchange_dir = tmp_path / "okx"
    exchange_dir.mkdir()
    exchange_dir.joinpath("README.txt").write_text("not market data")

    report = MarketDataCatalog(market_data_dir=tmp_path).inspect()

    assert report.status == "unsupported"
    assert report.blockers == [f"no usable local market data files found under {tmp_path}"]
    assert len(report.entries) == 1
    assert report.entries[0].status == "unsupported"
    assert report.entries[0].reason == "unsupported file extension"


def test_market_data_catalog_reports_incomplete_files(tmp_path) -> None:
    exchange_dir = tmp_path / "okx"
    exchange_dir.mkdir()
    exchange_dir.joinpath("BTC_USDT.feather").write_bytes(b"missing timeframe")
    exchange_dir.joinpath("ETH_USDT-1h.feather").write_bytes(b"")

    report = MarketDataCatalog(market_data_dir=tmp_path).inspect()

    assert report.status == "incomplete"
    assert report.blockers == [f"no usable local market data files found under {tmp_path}"]
    assert [(entry.status, entry.reason) for entry in report.entries] == [
        ("incomplete", "could not infer pair and timeframe from filename"),
        ("incomplete", "market data file is empty"),
    ]
