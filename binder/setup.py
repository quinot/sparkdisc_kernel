from setuptools import setup, find_packages

setup(
    name='sparkdisc_kernel',
    version='1.1',
    packages=find_packages(),
    description='SPARK Discovery Jupyter kernel',
    author='AdaCore',
    author_email='xxx@adacore.com',
    install_requires=[
        'jupyter_client', 'IPython', 'ipykernel',
        'colorama'
    ],
)
