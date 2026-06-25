/*M!999999\- enable the sandbox mode */ 
-- MariaDB dump 10.19  Distrib 10.11.14-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: 127.0.0.1    Database: psvibe_api
-- ------------------------------------------------------
-- Server version	8.0.46

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `accounts`
--

DROP TABLE IF EXISTS `accounts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `accounts` (
  `id` int NOT NULL AUTO_INCREMENT,
  `account_name` varchar(100) DEFAULT NULL,
  `account_type` varchar(50) DEFAULT NULL,
  `balance` decimal(15,2) DEFAULT '0.00',
  `notes` text,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=411 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `asset_disposals`
--

DROP TABLE IF EXISTS `asset_disposals`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `asset_disposals` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `asset_id` int NOT NULL,
  `disposed_qty` int NOT NULL DEFAULT '1',
  `disposal_amount` decimal(12,0) DEFAULT '0',
  `disposal_date` date DEFAULT NULL,
  `book_value_at_disposal` decimal(12,0) DEFAULT '0',
  `profit_loss` decimal(12,0) DEFAULT NULL,
  `note` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `attendance_log`
--

DROP TABLE IF EXISTS `attendance_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `attendance_log` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `staff_id` int NOT NULL,
  `check_in` datetime DEFAULT NULL,
  `check_out` datetime DEFAULT NULL,
  `hours_worked` decimal(5,2) DEFAULT NULL,
  `hourly_rate` decimal(10,2) DEFAULT NULL,
  `daily_pay` decimal(12,2) DEFAULT NULL,
  `date` date DEFAULT NULL,
  `status` varchar(20) DEFAULT 'checked_in',
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_staff_date` (`staff_id`,`date`),
  KEY `idx_date` (`date`),
  KEY `idx_status` (`status`),
  KEY `idx_attendance_log_branch` (`branch_id`),
  CONSTRAINT `attendance_log_ibfk_1` FOREIGN KEY (`staff_id`) REFERENCES `staff_records` (`staff_id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=12 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `branches`
--

DROP TABLE IF EXISTS `branches`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `branches` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) NOT NULL,
  `code` varchar(20) NOT NULL,
  `address` text,
  `phone` varchar(50) DEFAULT NULL,
  `is_active` tinyint(1) DEFAULT '1',
  `open_time` time DEFAULT '10:00:00',
  `close_time` time DEFAULT '22:00:00',
  `telegram_group_id` varchar(50) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `code` (`code`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `capital_movements`
--

DROP TABLE IF EXISTS `capital_movements`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `capital_movements` (
  `id` int NOT NULL AUTO_INCREMENT,
  `shareholder_id` int NOT NULL,
  `shareholder_name` varchar(100) NOT NULL,
  `type` enum('inject','eject') NOT NULL,
  `amount` decimal(15,0) NOT NULL,
  `payment_method` varchar(50) DEFAULT NULL,
  `payment_account` varchar(50) DEFAULT NULL,
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=7 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `card_wallet`
--

DROP TABLE IF EXISTS `card_wallet`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `card_wallet` (
  `id` int NOT NULL AUTO_INCREMENT,
  `member_id` varchar(50) DEFAULT NULL,
  `member_name` varchar(100) DEFAULT NULL,
  `phone` varchar(20) DEFAULT NULL,
  `balance_mins` decimal(10,2) DEFAULT NULL,
  `tier` varchar(20) DEFAULT NULL,
  `total_spend` decimal(10,2) DEFAULT NULL,
  `last_topup` datetime DEFAULT NULL,
  `last_updated` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `member_id` (`member_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `cash_movements`
--

DROP TABLE IF EXISTS `cash_movements`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `cash_movements` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `movement_type` enum('inject','eject','transfer_out','transfer_in','dividend') DEFAULT NULL,
  `account` varchar(50) NOT NULL,
  `amount` decimal(12,0) NOT NULL,
  `note` text,
  `staff_name` varchar(100) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=151 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `cash_transfers`
--

DROP TABLE IF EXISTS `cash_transfers`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `cash_transfers` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `from_account` varchar(50) NOT NULL,
  `to_account` varchar(50) NOT NULL,
  `amount` decimal(12,0) NOT NULL,
  `note` text,
  `created_by` varchar(100) DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=41 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `console_booking`
--

DROP TABLE IF EXISTS `console_booking`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `console_booking` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `console_id` varchar(20) DEFAULT NULL,
  `member_id` varchar(50) DEFAULT NULL,
  `booking_date` date DEFAULT NULL,
  `start_time` datetime DEFAULT NULL,
  `end_time` datetime DEFAULT NULL,
  `status` varchar(20) DEFAULT NULL,
  `staff_name` varchar(100) DEFAULT NULL,
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `telegram_chat_id` varchar(50) DEFAULT '',
  `duration_mins` int DEFAULT '0',
  `phone` varchar(50) DEFAULT '',
  `game_name` varchar(200) DEFAULT '',
  PRIMARY KEY (`id`),
  KEY `idx_booking_date` (`booking_date`),
  KEY `idx_booking_status` (`status`),
  KEY `idx_console_booking_branch` (`branch_id`),
  KEY `idx_console_status` (`console_id`,`status`),
  KEY `idx_date_console_time` (`booking_date`,`console_id`,`start_time`),
  KEY `idx_console_booking_console_id` (`console_id`)
) ENGINE=InnoDB AUTO_INCREMENT=780 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `console_games`
--

DROP TABLE IF EXISTS `console_games`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `console_games` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `console_id` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `console_name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `game_id` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `game_title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `genre` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT 'active',
  `install_type` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `slot_position` int DEFAULT '0',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_console_id` (`console_id`),
  KEY `idx_game_id` (`game_id`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB AUTO_INCREMENT=385 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `console_games_backup`
--

DROP TABLE IF EXISTS `console_games_backup`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `console_games_backup` (
  `id` int NOT NULL DEFAULT '0',
  `console_id` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `console_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `game_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `game_title` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `genre` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT 'active',
  `slot_position` int DEFAULT '0',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `console_status`
--

DROP TABLE IF EXISTS `console_status`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `console_status` (
  `console_id` varchar(20) NOT NULL,
  `status` varchar(50) DEFAULT NULL,
  `console_type` varchar(50) DEFAULT '',
  `current_game` text,
  `current_member` varchar(100) DEFAULT NULL,
  `start_time` datetime DEFAULT NULL,
  `last_updated` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `branch_id` int NOT NULL DEFAULT '1',
  PRIMARY KEY (`console_id`),
  KEY `idx_console_status_branch` (`branch_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb3 */ ;
/*!50003 SET character_set_results = utf8mb3 */ ;
/*!50003 SET collation_connection  = utf8mb3_general_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`root`@`%`*/ /*!50003 TRIGGER `trg_console_status_audit` AFTER UPDATE ON `console_status` FOR EACH ROW BEGIN
    IF NEW.status != OLD.status THEN
        INSERT INTO console_status_audit (console_id, old_status, new_status) 
        VALUES (NEW.console_id, OLD.status, NEW.status);
    END IF;
END */;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;

--
-- Table structure for table `console_status_audit`
--

DROP TABLE IF EXISTS `console_status_audit`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `console_status_audit` (
  `id` int NOT NULL AUTO_INCREMENT,
  `console_id` varchar(20) DEFAULT NULL,
  `old_status` varchar(20) DEFAULT NULL,
  `new_status` varchar(20) DEFAULT NULL,
  `changed_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=274 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `customer_bot_activity_log`
--

DROP TABLE IF EXISTS `customer_bot_activity_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `customer_bot_activity_log` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `tg_id` varchar(64) NOT NULL,
  `activity_type` varchar(32) NOT NULL,
  `detail` varchar(512) DEFAULT '',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_tg_id` (`tg_id`),
  KEY `idx_activity_type` (`activity_type`),
  KEY `idx_created_at` (`created_at`)
) ENGINE=InnoDB AUTO_INCREMENT=6010 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `customer_bot_users`
--

DROP TABLE IF EXISTS `customer_bot_users`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `customer_bot_users` (
  `tg_id` varchar(64) NOT NULL,
  `username` varchar(255) DEFAULT '',
  `first_name` varchar(255) DEFAULT '',
  `first_seen` datetime DEFAULT CURRENT_TIMESTAMP,
  `last_seen` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `total_interactions` int DEFAULT '1',
  `ai_chat_count` int DEFAULT '0',
  `last_command` varchar(64) DEFAULT '',
  `last_activity_type` varchar(32) DEFAULT 'message',
  `member_id` varchar(50) DEFAULT NULL,
  `member_name` varchar(200) DEFAULT NULL,
  `phone` varchar(50) DEFAULT NULL,
  `balance_mins` int DEFAULT '0',
  `is_active` tinyint(1) DEFAULT '1',
  `session_count` int DEFAULT '0',
  `last_session_date` datetime DEFAULT NULL,
  `booking_count` int DEFAULT '0',
  `total_spend_ks` decimal(12,0) DEFAULT '0',
  `last_booking_date` datetime DEFAULT NULL,
  `tags` varchar(255) DEFAULT '',
  `last_funnel_step` varchar(64) DEFAULT '',
  `frustration_count` int DEFAULT '0',
  PRIMARY KEY (`tg_id`),
  KEY `idx_last_seen` (`last_seen`),
  KEY `idx_total_interactions` (`total_interactions`),
  KEY `idx_member_id` (`member_id`),
  KEY `idx_phone` (`phone`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `customer_feedback`
--

DROP TABLE IF EXISTS `customer_feedback`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `customer_feedback` (
  `id` int NOT NULL AUTO_INCREMENT,
  `tg_id` varchar(64) NOT NULL,
  `username` varchar(255) DEFAULT '',
  `booking_id` varchar(50) DEFAULT '',
  `rating` int NOT NULL DEFAULT '0',
  `comment` text,
  `console_id` varchar(50) DEFAULT '',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_tg_id` (`tg_id`),
  KEY `idx_rating` (`rating`),
  KEY `idx_created_at` (`created_at`)
) ENGINE=InnoDB AUTO_INCREMENT=6 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `dashboard_users`
--

DROP TABLE IF EXISTS `dashboard_users`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `dashboard_users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(50) NOT NULL,
  `password_hash` varchar(255) NOT NULL,
  `role` enum('admin','staff') NOT NULL DEFAULT 'staff',
  `display_name` varchar(100) DEFAULT NULL,
  `staff_id` varchar(50) DEFAULT NULL,
  `is_active` tinyint(1) DEFAULT '1',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `last_login` timestamp NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB AUTO_INCREMENT=7 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `dividends`
--

DROP TABLE IF EXISTS `dividends`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `dividends` (
  `id` int NOT NULL AUTO_INCREMENT,
  `shareholder_id` int NOT NULL,
  `amount` decimal(15,0) NOT NULL,
  `dividend_date` date NOT NULL,
  `payment_method` varchar(50) DEFAULT 'cash',
  `status` varchar(20) DEFAULT 'paid',
  `notes` text,
  `recorded_by` varchar(100) DEFAULT 'system',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `shareholder_id` (`shareholder_id`),
  CONSTRAINT `dividends_ibfk_1` FOREIGN KEY (`shareholder_id`) REFERENCES `shareholders` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=7 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `finance_advances`
--

DROP TABLE IF EXISTS `finance_advances`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `finance_advances` (
  `id` int NOT NULL AUTO_INCREMENT,
  `member_id` varchar(50) DEFAULT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `advance_date` date DEFAULT NULL,
  `settle_date` date DEFAULT NULL,
  `status` varchar(20) DEFAULT 'pending',
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=148 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `finance_assets`
--

DROP TABLE IF EXISTS `finance_assets`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `finance_assets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(200) DEFAULT NULL,
  `purchase_date` date DEFAULT NULL,
  `per_price` decimal(15,2) DEFAULT NULL,
  `qty` int DEFAULT '1',
  `disposed_qty` int DEFAULT '0',
  `amount` decimal(15,2) DEFAULT NULL,
  `payment_method` varchar(100) DEFAULT NULL,
  `salvage_value` decimal(15,2) DEFAULT NULL,
  `useful_life` int DEFAULT NULL,
  `monthly_dep` decimal(15,2) DEFAULT NULL,
  `months_elapsed` int DEFAULT '0',
  `acc_depreciation` decimal(15,2) DEFAULT '0.00',
  `book_value` decimal(15,2) DEFAULT NULL,
  `notes` text,
  `status` varchar(20) DEFAULT 'active',
  `disposal_amount` decimal(15,2) DEFAULT NULL,
  `disposal_date` date DEFAULT NULL,
  `profit_loss` decimal(15,2) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_asset` (`name`(150),`purchase_date`,`per_price`)
) ENGINE=InnoDB AUTO_INCREMENT=3823 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `finance_opex_log`
--

DROP TABLE IF EXISTS `finance_opex_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `finance_opex_log` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `date` date DEFAULT NULL,
  `category` varchar(100) DEFAULT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `description` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `finance_payables`
--

DROP TABLE IF EXISTS `finance_payables`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `finance_payables` (
  `id` int NOT NULL AUTO_INCREMENT,
  `payee` varchar(200) DEFAULT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `due_date` date DEFAULT NULL,
  `status` varchar(20) DEFAULT 'pending',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `finance_prepaid`
--

DROP TABLE IF EXISTS `finance_prepaid`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `finance_prepaid` (
  `id` int NOT NULL AUTO_INCREMENT,
  `description` varchar(200) DEFAULT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `settle_date` date DEFAULT NULL,
  `status` varchar(20) DEFAULT 'pending',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=22 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `finance_receivables`
--

DROP TABLE IF EXISTS `finance_receivables`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `finance_receivables` (
  `id` int NOT NULL AUTO_INCREMENT,
  `payer` varchar(200) DEFAULT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `due_date` date DEFAULT NULL,
  `status` varchar(20) DEFAULT 'pending',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `food_cart`
--

DROP TABLE IF EXISTS `food_cart`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `food_cart` (
  `id` int NOT NULL AUTO_INCREMENT,
  `booking_id` varchar(50) NOT NULL,
  `item_name` varchar(255) NOT NULL,
  `quantity` int NOT NULL DEFAULT '1',
  `unit_price` decimal(10,0) NOT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `fulfilled_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_booking_id` (`booking_id`),
  KEY `idx_fulfilled` (`fulfilled_at`)
) ENGINE=InnoDB AUTO_INCREMENT=38 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `games_library`
--

DROP TABLE IF EXISTS `games_library`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `games_library` (
  `game_title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `genre` varchar(100) DEFAULT NULL,
  `solo_multi` varchar(50) DEFAULT '',
  `final_status` text,
  `disc_count` int DEFAULT '0',
  `last_updated` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`game_title`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `games_library_backup`
--

DROP TABLE IF EXISTS `games_library_backup`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `games_library_backup` (
  `game_title` varchar(200) NOT NULL,
  `genre` varchar(100) DEFAULT NULL,
  `solo_multi` varchar(50) DEFAULT '',
  `final_status` text,
  `disc_count` int DEFAULT '0',
  `last_updated` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `inventory`
--

DROP TABLE IF EXISTS `inventory`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `inventory` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `item_name` varchar(200) DEFAULT NULL,
  `category` varchar(100) DEFAULT NULL,
  `quantity` int DEFAULT NULL,
  `unit_price` decimal(10,2) DEFAULT NULL,
  `reorder_level` int DEFAULT NULL,
  `last_updated` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_inventory_branch` (`branch_id`)
) ENGINE=InnoDB AUTO_INCREMENT=1785 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `loyalty_redemptions`
--

DROP TABLE IF EXISTS `loyalty_redemptions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `loyalty_redemptions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `member_id` varchar(50) NOT NULL,
  `reward_id` int NOT NULL,
  `reward_name` varchar(200) DEFAULT NULL,
  `points_used` int NOT NULL,
  `status` varchar(20) DEFAULT 'pending',
  `redeemed_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `redeemed_by` varchar(100) DEFAULT NULL,
  `notes` text,
  PRIMARY KEY (`id`),
  KEY `idx_member` (`member_id`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `loyalty_rewards`
--

DROP TABLE IF EXISTS `loyalty_rewards`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `loyalty_rewards` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(200) NOT NULL,
  `description` text,
  `points_required` int NOT NULL,
  `reward_type` varchar(50) NOT NULL,
  `reward_value` decimal(10,2) DEFAULT NULL,
  `stock` int DEFAULT '-1',
  `is_active` tinyint(1) DEFAULT '1',
  `image_url` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=6 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `loyalty_settings`
--

DROP TABLE IF EXISTS `loyalty_settings`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `loyalty_settings` (
  `id` int NOT NULL AUTO_INCREMENT,
  `points_per_ks` decimal(10,2) NOT NULL DEFAULT '1.00',
  `points_per_min` decimal(10,2) NOT NULL DEFAULT '0.50',
  `min_redeem_points` int NOT NULL DEFAULT '100',
  `points_per_ks_redeem` decimal(10,2) NOT NULL DEFAULT '1.00',
  `signup_bonus_points` int NOT NULL DEFAULT '50',
  `birthday_bonus_points` int NOT NULL DEFAULT '100',
  `referral_points` int NOT NULL DEFAULT '25',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `loyalty_transactions`
--

DROP TABLE IF EXISTS `loyalty_transactions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `loyalty_transactions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `member_id` varchar(50) NOT NULL,
  `points` int NOT NULL,
  `transaction_type` varchar(50) NOT NULL,
  `reference_id` varchar(100) DEFAULT NULL,
  `description` text,
  `branch_id` int DEFAULT '1',
  `created_by` varchar(100) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_member` (`member_id`),
  KEY `idx_type` (`transaction_type`),
  KEY `idx_date` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `member_coupons`
--

DROP TABLE IF EXISTS `member_coupons`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `member_coupons` (
  `id` int NOT NULL AUTO_INCREMENT,
  `coupon_code` varchar(20) NOT NULL,
  `member_id` varchar(50) NOT NULL,
  `original_minutes` int NOT NULL DEFAULT '0',
  `balance_minutes` int NOT NULL DEFAULT '0',
  `issued_date` datetime NOT NULL,
  `expiry_date` datetime NOT NULL,
  `status` varchar(20) NOT NULL DEFAULT 'active',
  `promo_id` int DEFAULT NULL,
  `source_session_id` int DEFAULT NULL,
  `redeemed_at` datetime DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `coupon_code` (`coupon_code`),
  KEY `promo_id` (`promo_id`),
  KEY `idx_member_coupons_member` (`member_id`),
  KEY `idx_member_coupons_status` (`status`),
  CONSTRAINT `member_coupons_ibfk_1` FOREIGN KEY (`promo_id`) REFERENCES `promotions` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=28 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `member_loyalty`
--

DROP TABLE IF EXISTS `member_loyalty`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `member_loyalty` (
  `id` int NOT NULL AUTO_INCREMENT,
  `member_id` varchar(50) NOT NULL,
  `total_points` int NOT NULL DEFAULT '0',
  `redeemed_points` int NOT NULL DEFAULT '0',
  `available_points` int GENERATED ALWAYS AS ((`total_points` - `redeemed_points`)) STORED,
  `lifetime_spent` decimal(12,2) NOT NULL DEFAULT '0.00',
  `tier` varchar(50) DEFAULT 'Bronze',
  `join_date` date DEFAULT NULL,
  `birthday` date DEFAULT NULL,
  `last_earn_date` date DEFAULT NULL,
  `last_redeem_date` date DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `member_id` (`member_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `member_wallets`
--

DROP TABLE IF EXISTS `member_wallets`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `member_wallets` (
  `member_id` varchar(50) NOT NULL,
  `branch_id` int NOT NULL DEFAULT '1',
  `balance_mins` int DEFAULT '0',
  `total_bought_mins` int DEFAULT '0',
  `total_used_mins` int DEFAULT '0',
  `member_name` varchar(200) DEFAULT NULL,
  `phone` varchar(50) DEFAULT NULL,
  `effective_rate` decimal(10,2) DEFAULT '1.00',
  `tier` varchar(50) DEFAULT NULL,
  `total_spend` decimal(15,2) DEFAULT '0.00',
  `last_updated` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `lifetime_spend` decimal(12,2) DEFAULT '0.00',
  `ranking_net_spend` decimal(12,2) DEFAULT '0.00',
  `reg_staff` varchar(100) DEFAULT NULL,
  `referral_code` varchar(50) DEFAULT NULL,
  `join_date` date DEFAULT NULL,
  PRIMARY KEY (`member_id`),
  KEY `idx_member_wallets_member` (`member_id`),
  KEY `idx_branch_id` (`branch_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `members`
--

DROP TABLE IF EXISTS `members`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `members` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `member_id` varchar(50) NOT NULL,
  `name` varchar(255) DEFAULT NULL,
  `phone` varchar(50) DEFAULT NULL,
  `balance_minutes` decimal(10,2) DEFAULT '0.00',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `member_id` (`member_id`),
  KEY `idx_branch_id` (`branch_id`)
) ENGINE=InnoDB AUTO_INCREMENT=114 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `opex`
--

DROP TABLE IF EXISTS `opex`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `opex` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `category` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `description` text COLLATE utf8mb4_unicode_ci,
  `amount` int NOT NULL,
  `payment_method` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT 'Cash',
  `recorded_by` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `expense_date` date NOT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_date` (`expense_date`),
  KEY `idx_category` (`category`),
  KEY `idx_opex_branch` (`branch_id`)
) ENGINE=InnoDB AUTO_INCREMENT=37 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `prepaid_amortization`
--

DROP TABLE IF EXISTS `prepaid_amortization`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `prepaid_amortization` (
  `id` int NOT NULL AUTO_INCREMENT,
  `prepaid_id` int NOT NULL,
  `period` varchar(7) NOT NULL COMMENT 'YYYY-MM',
  `amount` decimal(15,2) NOT NULL DEFAULT '0.00',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_period` (`period`),
  KEY `idx_prepaid` (`prepaid_id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `profit_distribution_details`
--

DROP TABLE IF EXISTS `profit_distribution_details`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `profit_distribution_details` (
  `id` int NOT NULL AUTO_INCREMENT,
  `distribution_id` int NOT NULL,
  `shareholder_id` int NOT NULL,
  `shareholder_name` varchar(100) NOT NULL,
  `share_amount` decimal(15,0) NOT NULL DEFAULT '0',
  `share_pct` decimal(5,2) NOT NULL DEFAULT '0.00',
  `is_management_fee` tinyint(1) DEFAULT '0',
  PRIMARY KEY (`id`),
  KEY `distribution_id` (`distribution_id`),
  CONSTRAINT `profit_distribution_details_ibfk_1` FOREIGN KEY (`distribution_id`) REFERENCES `profit_distributions` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `profit_distributions`
--

DROP TABLE IF EXISTS `profit_distributions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `profit_distributions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `distribution_date` date NOT NULL,
  `period_label` varchar(50) DEFAULT NULL,
  `net_profit` decimal(15,0) NOT NULL DEFAULT '0',
  `management_fee` decimal(15,0) NOT NULL DEFAULT '0',
  `distributable_amount` decimal(15,0) NOT NULL DEFAULT '0',
  `status` enum('pending','distributed','cancelled') DEFAULT 'pending',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `promotions`
--

DROP TABLE IF EXISTS `promotions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `promotions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) DEFAULT NULL,
  `promo_type` varchar(20) DEFAULT 'cashback_coupon',
  `promo_name` varchar(100) DEFAULT NULL,
  `discount_type` varchar(50) DEFAULT NULL,
  `discount_value` decimal(10,2) DEFAULT NULL,
  `start_date` date DEFAULT NULL,
  `end_date` date DEFAULT NULL,
  `coupon_expiry_date` date DEFAULT NULL,
  `status` varchar(20) DEFAULT NULL,
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=519 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `promotions_log`
--

DROP TABLE IF EXISTS `promotions_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `promotions_log` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `voucher_no` varchar(50) DEFAULT NULL,
  `promo_id` varchar(50) DEFAULT NULL,
  `promo_title` varchar(200) DEFAULT NULL,
  `member_id` varchar(50) DEFAULT NULL,
  `console_id` varchar(20) DEFAULT NULL,
  `gross_total` decimal(10,2) DEFAULT '0.00',
  `discount_amt` decimal(10,2) DEFAULT '0.00',
  `net_total` decimal(10,2) DEFAULT '0.00',
  `staff_name` varchar(100) DEFAULT NULL,
  `promo_date` date DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `receipts`
--

DROP TABLE IF EXISTS `receipts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `receipts` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `receipt_no` varchar(50) DEFAULT NULL,
  `member_id` varchar(50) DEFAULT NULL,
  `amount` decimal(10,2) DEFAULT NULL,
  `payment_method` varchar(50) DEFAULT NULL,
  `items` text,
  `receipt_date` datetime DEFAULT NULL,
  `staff_name` varchar(100) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `receipt_no` (`receipt_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `referral_log`
--

DROP TABLE IF EXISTS `referral_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `referral_log` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `member_id` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `referrer_id` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `referral_code` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `referred_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `reward_granted` int DEFAULT '0',
  `reward_amount` decimal(10,2) DEFAULT '0.00',
  `source` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT 'customer_bot',
  `notes` text COLLATE utf8mb4_unicode_ci,
  PRIMARY KEY (`id`),
  KEY `idx_member_id` (`member_id`),
  KEY `idx_referrer_id` (`referrer_id`),
  KEY `idx_referral_code` (`referral_code`)
) ENGINE=InnoDB AUTO_INCREMENT=199 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `salary_advance`
--

DROP TABLE IF EXISTS `salary_advance`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `salary_advance` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `staff_name` varchar(100) DEFAULT NULL,
  `amount` decimal(10,2) DEFAULT NULL,
  `advance_date` date DEFAULT NULL,
  `repayment_status` varchar(20) DEFAULT NULL,
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `salary_payroll`
--

DROP TABLE IF EXISTS `salary_payroll`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `salary_payroll` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `staff_name` varchar(100) DEFAULT NULL,
  `base_salary` decimal(10,2) DEFAULT NULL,
  `bonus` decimal(10,2) DEFAULT NULL,
  `deductions` decimal(10,2) DEFAULT NULL,
  `net_salary` decimal(10,2) DEFAULT NULL,
  `pay_period` varchar(50) DEFAULT NULL,
  `pay_date` date DEFAULT NULL,
  `status` varchar(20) DEFAULT NULL,
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `sales_daily`
--

DROP TABLE IF EXISTS `sales_daily`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `sales_daily` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `voucher_no` varchar(50) DEFAULT NULL,
  `sale_date` date DEFAULT NULL,
  `console_id` varchar(20) DEFAULT NULL,
  `member_id` varchar(50) DEFAULT NULL,
  `amount` decimal(10,2) DEFAULT NULL,
  `gross` decimal(10,2) DEFAULT '0.00',
  `discount` decimal(10,2) DEFAULT '0.00',
  `net` decimal(10,2) DEFAULT '0.00',
  `staff_name` varchar(100) DEFAULT NULL,
  `payment_method` varchar(50) DEFAULT NULL,
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_sales_daily_date` (`sale_date`),
  KEY `idx_sales_daily_created` (`created_at`),
  KEY `idx_sales_daily_branch` (`branch_id`)
) ENGINE=InnoDB AUTO_INCREMENT=412 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `sales_daily_backup_20260617`
--

DROP TABLE IF EXISTS `sales_daily_backup_20260617`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `sales_daily_backup_20260617` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `voucher_no` varchar(50) DEFAULT NULL,
  `sale_date` date DEFAULT NULL,
  `console_id` varchar(20) DEFAULT NULL,
  `member_id` varchar(50) DEFAULT NULL,
  `amount` decimal(10,2) DEFAULT NULL,
  `gross` decimal(10,2) DEFAULT '0.00',
  `discount` decimal(10,2) DEFAULT '0.00',
  `net` decimal(10,2) DEFAULT '0.00',
  `staff_name` varchar(100) DEFAULT NULL,
  `payment_method` varchar(50) DEFAULT NULL,
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_sales_daily_date` (`sale_date`),
  KEY `idx_sales_daily_created` (`created_at`),
  KEY `idx_sales_daily_branch` (`branch_id`)
) ENGINE=InnoDB AUTO_INCREMENT=231 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `settings`
--

DROP TABLE IF EXISTS `settings`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `settings` (
  `id` int NOT NULL AUTO_INCREMENT,
  `setting_key` varchar(100) DEFAULT NULL,
  `setting_value` text,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `setting_key` (`setting_key`)
) ENGINE=InnoDB AUTO_INCREMENT=7428 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `settings_config`
--

DROP TABLE IF EXISTS `settings_config`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `settings_config` (
  `id` int NOT NULL AUTO_INCREMENT,
  `config_key` varchar(100) NOT NULL,
  `config_value` text NOT NULL,
  `config_type` enum('string','int','float','json','bool') DEFAULT 'string',
  `category` varchar(50) DEFAULT 'general',
  `description` varchar(255) DEFAULT '',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `config_key` (`config_key`),
  KEY `idx_key` (`config_key`),
  KEY `idx_category` (`category`)
) ENGINE=InnoDB AUTO_INCREMENT=18948 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `shareholders`
--

DROP TABLE IF EXISTS `shareholders`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `shareholders` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) NOT NULL,
  `role` varchar(50) DEFAULT 'Shareholder',
  `capital_contribution` decimal(15,0) DEFAULT '0',
  `ownership_pct` decimal(5,2) DEFAULT '0.00',
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `staff_records`
--

DROP TABLE IF EXISTS `staff_records`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `staff_records` (
  `staff_id` int NOT NULL AUTO_INCREMENT,
  `staff_name` varchar(200) NOT NULL,
  `base_salary` decimal(12,2) DEFAULT '0.00',
  `hourly_rate` decimal(10,2) DEFAULT NULL,
  `role` varchar(100) DEFAULT NULL,
  `is_active` tinyint(1) DEFAULT '1',
  `last_updated` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `branch_id` int NOT NULL DEFAULT '1',
  PRIMARY KEY (`staff_id`),
  UNIQUE KEY `idx_staff_name` (`staff_name`),
  KEY `idx_staff_records_branch` (`branch_id`)
) ENGINE=InnoDB AUTO_INCREMENT=5864 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `staff_records_bak`
--

DROP TABLE IF EXISTS `staff_records_bak`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `staff_records_bak` (
  `staff_id` int NOT NULL DEFAULT '0',
  `staff_name` varchar(200) NOT NULL,
  `base_salary` decimal(12,2) DEFAULT '0.00',
  `role` varchar(100) DEFAULT NULL,
  `is_active` tinyint(1) DEFAULT '1',
  `last_updated` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY `idx_unique_staff` (`staff_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `stock_hold`
--

DROP TABLE IF EXISTS `stock_hold`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `stock_hold` (
  `id` int NOT NULL AUTO_INCREMENT,
  `booking_id` varchar(64) NOT NULL,
  `item_name` varchar(255) NOT NULL,
  `quantity` int NOT NULL DEFAULT '0',
  `unit_price` decimal(10,0) NOT NULL DEFAULT '0',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `status` varchar(16) DEFAULT 'held',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_booking_item` (`booking_id`,`item_name`,`status`),
  KEY `idx_booking_id` (`booking_id`),
  KEY `idx_item_name` (`item_name`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB AUTO_INCREMENT=17 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `stock_in`
--

DROP TABLE IF EXISTS `stock_in`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `stock_in` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `batch_id` varchar(50) NOT NULL,
  `item_name` varchar(200) NOT NULL,
  `quantity` int NOT NULL DEFAULT '0',
  `unit_cost` decimal(10,2) NOT NULL DEFAULT '0.00',
  `total_cost` decimal(12,2) GENERATED ALWAYS AS ((`quantity` * `unit_cost`)) STORED,
  `source` varchar(100) DEFAULT '',
  `receipt_no` varchar(100) DEFAULT '',
  `payment_method` varchar(50) DEFAULT '',
  `paid_by` varchar(100) DEFAULT '',
  `staff_name` varchar(100) DEFAULT '',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `batch_id` (`batch_id`)
) ENGINE=InnoDB AUTO_INCREMENT=81 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `stock_out`
--

DROP TABLE IF EXISTS `stock_out`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `stock_out` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `item_name` varchar(200) DEFAULT NULL,
  `quantity` int DEFAULT NULL,
  `unit_price` decimal(10,2) DEFAULT NULL,
  `total` decimal(10,2) DEFAULT NULL,
  `sale_date` datetime DEFAULT NULL,
  `staff_name` varchar(100) DEFAULT NULL,
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=517 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `sync_status`
--

DROP TABLE IF EXISTS `sync_status`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `sync_status` (
  `id` int NOT NULL AUTO_INCREMENT,
  `sheet_name` varchar(100) NOT NULL,
  `last_sync_at` datetime NOT NULL,
  `rows_synced` int DEFAULT '0',
  `status` varchar(20) DEFAULT 'pending',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_sheet` (`sheet_name`)
) ENGINE=InnoDB AUTO_INCREMENT=25702 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `topup_log`
--

DROP TABLE IF EXISTS `topup_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `topup_log` (
  `id` int NOT NULL AUTO_INCREMENT,
  `branch_id` int NOT NULL DEFAULT '1',
  `member_id` varchar(50) DEFAULT NULL,
  `amount` decimal(10,2) DEFAULT NULL,
  `mins_added` int DEFAULT '0',
  `topup_date` datetime DEFAULT NULL,
  `staff_name` varchar(100) DEFAULT NULL,
  `payment_method` varchar(50) DEFAULT NULL,
  `balance_before` decimal(10,2) DEFAULT NULL,
  `balance_after` decimal(10,2) DEFAULT NULL,
  `balance_mins_before` int DEFAULT '0',
  `balance_mins_after` int DEFAULT '0',
  `notes` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_topup_log_date` (`created_at`),
  KEY `idx_topup_log_member` (`member_id`),
  KEY `idx_topup_log_branch` (`branch_id`)
) ENGINE=InnoDB AUTO_INCREMENT=8 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-06-22  3:06:51
