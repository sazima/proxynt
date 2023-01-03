import glob
import os.path

package_name = 'proxynt'
version='0.0.2'
import setuptools
l = setuptools.find_packages(where='.')
l = [package_name + '.'+ x for x in l]
l.append(package_name)

html_file = glob.glob(os.path.join('server/template', '**.html'))
js_file = glob.glob(os.path.join('server/template/js', '**.js'))
css_file = glob.glob(os.path.join('server/template/css', '**.css'))
font_file = glob.glob(os.path.join('server/template/css/fonts', '**.woff'))

setuptools.setup(
    name=package_name,
    version=version,
    package_dir={
        package_name: '.',
    },
    data_files=[
        (f'{package_name}/server/template', html_file),
        (f'{package_name}/server/template/js', js_file),
        (f'{package_name}/server/template/css', css_file),
        (f'{package_name}/server/template/css/fonts', font_file),
    ],
    entry_points=f"""
    [console_scripts]
    nt_client = {package_name}.run_client:main
    nt_server = {package_name}.run_server:main
    """,
    packages=l,
    install_requires=['tornado',  'typing_extensions']
)
