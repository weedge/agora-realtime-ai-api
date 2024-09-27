from setuptools import setup, find_packages

setup(
    name='agora-realtime-ai-api',
    version='1.0.0',
    author='agora.io',
    author_email='apps_stuff@agora.io',
    description='Agora\'s low latency/top quality Realtime API to work with Voice Conversational AI',
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    url='https://github.com/AgoraIO/agora-realtime-ai-api',
    packages=find_packages(),
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.11',
    install_requires=[
        'pyee==12.0.0',
        'agora-python-server-sdk==2.0.2'
    ],  # List of dependencies (if any)
)