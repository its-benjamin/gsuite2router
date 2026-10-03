from setuptools import setup, find_packages

setup(
    name="gsuite2router",
    version="1.1.0",
    description="Bulk-add Google Workspace accounts to 9Router Antigravity, Cline, and Kilo Code providers via browser-automated OAuth",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    packages=find_packages(),
    install_requires=[
        "DrissionPage>=4.0",
    ],
    python_requires=">=3.8",
    entry_points={
        "console_scripts": [
            "gsuite2router=gsuite2router.cli:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
)
