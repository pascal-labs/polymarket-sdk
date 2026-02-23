from setuptools import setup, find_packages

setup(
    name="polymarket-sdk",
    version="0.1.0",
    description="Python SDK for Polymarket CLOB API interaction, order management, and market data",
    author="Pascal Legate",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=[
        "py-clob-client>=0.17.0",
        "eth-account>=0.11.0",
        "web3>=6.0",
        "websockets>=12.0",
        "requests>=2.31",
        "python-dotenv>=1.0",
    ],
)
