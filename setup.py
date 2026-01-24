import sys

from constant.system_constant import SystemConstant
package_name = SystemConstant.PACKAGE_NAME
version = SystemConstant.VERSION

import setuptools
l = setuptools.find_packages(where='.')
l = [package_name + '.'+ x for x in l]
l.append(package_name)
with open("readme_en.md", "r", encoding="utf8") as f:
    readme = f.read()
readme = readme.replace('./readme.md', 'https://github.com/sazima/proxynt/blob/master/readme.md')

setuptools.setup(
    name=package_name,
    version=version,
    package_dir={
        package_name: '.',
    },
    long_description=readme,
    long_description_content_type="text/markdown",
    url='https://github.com/sazima/proxynt',
    include_package_data=True,
    entry_points=f"""
    [console_scripts]
    nt_client = {package_name}.run_client:main
    nt_server = {package_name}.run_server:main
    """,
    packages=l,

    install_requires=['tornado',
                      'typing_extensions',
                      'winloop>=0.1.0; sys_platform == "win32"',
                      'uvloop==0.22.0; sys_platform != "win32" and python_version >= "3.7"',
                      'uvloop<0.15; sys_platform != "win32" and python_version < "3.7"',
                      'xxhash>=3.0.0',
                      'msgpack',
                      ],
    extras_require={
        "snappy": ["python-snappy"],
    },
)
