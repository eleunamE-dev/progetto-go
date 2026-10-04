CREATE USER IF NOT EXISTS 'migrator'@'%' IDENTIFIED BY 'migrator-password';
GRANT ALL PRIVILEGES ON bookreviews.* TO 'migrator'@'%';

CREATE USER IF NOT EXISTS 'app'@'%' IDENTIFIED BY 'app-password';
GRANT SELECT, INSERT, UPDATE, DELETE ON bookreviews.* TO 'app'@'%';
