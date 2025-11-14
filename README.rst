============
Reservations
============

This is Django application used for room reservations at FRI.

Installation
============

1. Clone the repository.

   .. code-block:: bash

       git clone https://github.com/UL-FRI/reservations.git


2. Navigate to the project directory and install the required dependencies.

   .. code-block:: bash

       cd reservations
       uv sync

3. Create/update the database.

   .. code-block:: bash

       uv run manage.py migrate

4. Create superuser account.

   .. code-block:: bash

       uv run manage.py createsuperuser

5. Run the development server.
    .. code-block:: bash
    
         uv run manage.py runserver

Usage
=====

The administration interface can be accessed at ``localhhost:8000/admin/``.

The API endpoints are available at ``localhost:8000/api/``.
