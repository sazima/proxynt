import setuptools
l = setuptools.find_packages(where='.')
l = ['proxynt.' + x for x in l]
l.append('proxynt')


print(l)
setuptools.setup(
    name='proxynt',
    version='0.0.1',
    # zip_safe= False,
    package_dir={
        'proxynt': '.',
    },
    entry_points="""
    [console_scripts]
    run_client = proxynt.run_client:main
    run_server = proxynt.run_server:main
    """,
    packages=l,
    include_package_data=True,
    install_requires=['tornado',  'typing_extensions']
)
