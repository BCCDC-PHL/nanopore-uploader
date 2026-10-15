
from setuptools import setup, find_packages

setup(
    name='nanopore-uploader',
    version='0.1.0',
    description='',
    author='Dan Fornika',
    author_email='dan.fornika@bccdc.ca',
    url='https://github.com/BCCDC-PHL/nanopore-uploader',
    packages=find_packages(exclude=('test', 'test.*')),
    python_requires='>=3.10',
    install_requires=[
        "paramiko~=2.11.0",
        "requests~=2.32",
        "jinja2~=3.1",
    ],
    extras_require={
        "tui": ["textual>=8.2"],
    },
    setup_requires=[],
    tests_require=[],
    entry_points = {
        'console_scripts': ['nanopore-uploader=nanopore_uploader.__main__:main'],
    }
)
