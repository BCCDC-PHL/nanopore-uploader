
from setuptools import setup, find_packages

import nanopore_uploader

setup(
    name='nanopore-uploader',
    version=nanopore_uploader.__version__,
    description='',
    author='Dan Fornika',
    author_email='dan.fornika@bccdc.ca',
    url='https://github.com/BCCDC-PHL/nanopore-uploader',
    packages=find_packages(exclude=('test', 'test.*')),
    python_requires='>=3.10',
    install_requires=[
        "sqlalchemy==1.4.40",
        "alembic==1.8.0",
    ],
    setup_requires=[],
    tests_require=[],
    entry_points = {
        'console_scripts': ['nanopore-uploader=nanopore_uploader.__main__:main'],
    }
)
