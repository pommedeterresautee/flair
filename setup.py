from setuptools import setup, find_packages, Extension
from Cython.Build import cythonize, build_ext
# from setuptools import find_packages
# from distutils.extension import Extension
# from distutils.core import setup
# from Cython.Build import cythonize
# from Cython.Distutils import build_ext

extensions = [
    Extension("my_test", ["flair/models/resolve_char_ids.pyx"])
]

with open("requirements.txt") as f:
    required = f.read().splitlines()

setup(
    name="flair",
    version="0.4.3",
    description="A very simple framework for state-of-the-art NLP",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="Alan Akbik",
    author_email="alan.akbik@zalando.de",
    url="https://github.com/zalandoresearch/flair",
    packages=find_packages(exclude="tests"),  # same as name
    license="MIT",
    install_requires=required,
    include_package_data=True,
    python_requires=">=3.6",
    cmdclass={'build_ext': build_ext, "extra_compile_args": ["03"]},
    ext_modules=cythonize(extensions),
)
